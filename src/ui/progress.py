import threading
import os
import subprocess

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

import backend


class _ProgressSignals(QObject):
    update = Signal(str, float)
    done = Signal(bool, str)


class ProgressPage(QWidget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(12)
        title = QLabel("Installing System")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(title)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        self.progress_label = QLabel("Waiting...")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)
        layout.addStretch(1)

        self.main_window = None
        self.stop_requested = False
        self.installation_error = None
        self.target_root = "/mnt/sysimage"
        self.signals = _ProgressSignals()
        self.signals.update.connect(self._on_progress)
        self.signals.done.connect(self._on_done)

    def _on_progress(self, text, fraction):
        self.progress_label.setText(text)
        self.progress_bar.setValue(max(0, min(100, int(fraction * 100))))

    def _on_done(self, success, error):
        if success:
            self._on_progress("Installation finished successfully.", 1.0)
            QTimer.singleShot(1500, lambda: self.main_window.navigate_to_page("finished"))
        else:
            self._on_progress(f"Installation failed: {error}", self.progress_bar.value() / 100.0)

    def _update_progress_text(self, text, fraction=None):
        frac = self.progress_bar.value() / 100.0 if fraction is None else fraction
        self.signals.update.emit(text, frac)

    def _scaled_progress_callback(self, step_start, step_end):
        def cb(text, fraction=None):
            f = step_start if fraction is None else (step_start + (step_end - step_start) * float(fraction))
            self._update_progress_text(text, f)

        return cb

    def start_installation(self, main_window, config_data):
        self.main_window = main_window
        self.stop_requested = False
        self.progress_bar.setValue(0)
        self.progress_label.setText("Preparing installation...")
        t = threading.Thread(target=self._run_installation_steps, args=(config_data,), daemon=True)
        t.start()

    def _run_installation_steps(self, config_data):
        udisks_stopped_for_storage = False
        try:
            disk_config = config_data.get("disk", {})
            commands = disk_config.get("commands", [])
            partitions = disk_config.get("partitions", [])
            primary_disk = (disk_config.get("target_disks") or [None])[0]
            storage_cb = self._scaled_progress_callback(0.02, 0.2)

            if primary_disk and commands:
                stop_ok, stop_err = backend._stop_service("udisks2.service")
                if not stop_ok:
                    print(f"Warning: could not stop udisks2 (continuing): {stop_err}")
                else:
                    print("Stopped udisks2 temporarily for storage setup.")
                    udisks_stopped_for_storage = True
                swap_ok, swap_err = backend.swapoff_on_disk(primary_disk, storage_cb)
                if not swap_ok:
                    raise RuntimeError(
                        f"Could not turn off swap on the target disk. Details: {swap_err}"
                    )
                lvm_ok, lvm_err = backend._deactivate_lvm_on_disk(primary_disk, storage_cb)
                if not lvm_ok:
                    print(f"Warning: LVM deactivation incomplete (continuing): {lvm_err}")

            self._update_progress_text("Preparing storage...", 0.02)
            for idx, cmd in enumerate(commands):
                if (
                    primary_disk
                    and cmd
                    and cmd[0] == "wipefs"
                    and primary_disk in cmd
                ):
                    so2_ok, so2_err = backend.swapoff_on_disk(primary_disk, storage_cb)
                    if not so2_ok:
                        raise RuntimeError(
                            f"Could not turn off swap on the target disk before wiping. Details: {so2_err}"
                        )
                ok, err, _ = backend._run_command(
                    cmd,
                    f"Storage step {idx + 1}",
                    progress_callback=storage_cb,
                    timeout=120,
                )
                if not ok:
                    raise RuntimeError(err or f"Storage command failed: {' '.join(cmd)}")
                if (
                    ok
                    and primary_disk
                    and cmd
                    and cmd[0] == "wipefs"
                    and primary_disk in cmd
                ):
                    pp_ok, pp_err, _ = backend._run_command(
                        ["partprobe", primary_disk],
                        f"Reread partitions on {primary_disk} after wipefs",
                        progress_callback=storage_cb,
                        timeout=30,
                    )
                    if not pp_ok:
                        print(f"Warning: partprobe after wipefs failed: {pp_err}")
                    try:
                        subprocess.run(["udevadm", "settle"], check=False, timeout=15)
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        print(f"Warning: udevadm settle after wipefs: {e}")
                    try:
                        subprocess.run(["sync"], check=False, timeout=15)
                    except Exception:
                        pass
            if not backend.ensure_directory(self.target_root):
                raise RuntimeError(f"Could not create {self.target_root}")
            for part in sorted(partitions, key=lambda p: 0 if p.get("mountpoint") == "/" else 1):
                mountpoint = part.get("mountpoint")
                device = part.get("device")
                if not mountpoint or not device:
                    continue
                full_mount = os.path.join(self.target_root, mountpoint.lstrip("/"))
                if mountpoint != "/" and not backend.ensure_directory(full_mount):
                    raise RuntimeError(f"Could not create mountpoint {full_mount}")
                ok, err, _ = backend._run_command(
                    ["mount", device, self.target_root if mountpoint == "/" else full_mount],
                    f"Mount {device} at {mountpoint}",
                    progress_callback=self._scaled_progress_callback(0.2, 0.3),
                    timeout=60,
                )
                if not ok:
                    raise RuntimeError(err or f"Mount failed for {device}")

            copy_ok, copy_err = backend.copy_live_environment(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.3, 0.75),
            )
            if not copy_ok:
                raise RuntimeError(copy_err or "Live copy failed")

            cfg_ok, cfg_err = backend.configure_system_in_container(
                self.target_root,
                config_data,
                progress_callback=self._scaled_progress_callback(0.75, 0.87),
            )
            if not cfg_ok:
                raise RuntimeError(cfg_err or "System configuration failed")

            cleanup_ok, cleanup_err = backend.remove_live_users_and_configure_oobe(
                self.target_root,
                install_user_created=False,
                install_username=None,
                progress_callback=self._scaled_progress_callback(0.82, 0.90),
                btrfs_subvolumes=bool(disk_config.get("btrfs_subvolumes", False)),
            )
            if not cleanup_ok:
                raise RuntimeError(cleanup_err or "Failed to remove live users")

            efi = None
            for part in disk_config.get("partitions", []):
                if part.get("mountpoint") == "/boot/efi":
                    efi = part.get("device")
                    break
            boot_ok, boot_err, _ = backend.install_bootloader_in_container(
                self.target_root,
                primary_disk,
                efi,
                progress_callback=self._scaled_progress_callback(0.87, 0.99),
            )
            if not boot_ok:
                raise RuntimeError(boot_err or "Bootloader install failed")

            # v2 originally skipped post-copy finalization; that leaves live-boot
            # artifacts and can produce non-bootable installs.
            payload_cfg = config_data.get("payload", {}) if isinstance(config_data, dict) else {}
            disk_cfg = config_data.get("disk", {}) if isinstance(config_data, dict) else {}
            post_ok, post_err = backend.setup_live_environment_post_copy(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.94, 0.995),
                server_install=bool(payload_cfg.get("server_install", False)),
                btrfs_subvolumes=bool(disk_cfg.get("btrfs_subvolumes", False)),
            )
            if not post_ok:
                raise RuntimeError(post_err or "Post-copy system finalization failed")

            fstab_ok, fstab_err = backend.generate_fstab_for_target(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.995, 0.998),
            )
            if not fstab_ok:
                raise RuntimeError(fstab_err or "Failed to generate fstab")

            # Run OOBE setup LAST so no later stage can overwrite markers/units.
            ps_ok, ps_err = backend.configure_plasma_setup_oobe(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.997, 0.999),
            )
            if not ps_ok:
                raise RuntimeError(ps_err or "Plasma Setup OOBE configuration failed")

            backend.remove_centrio_installer()
            self.signals.done.emit(True, "")
        except Exception as e:
            self.installation_error = str(e)
            self.signals.done.emit(False, self.installation_error)
        finally:
            if udisks_stopped_for_storage:
                backend._start_service("udisks2.service")

    def stop_installation(self):
        self.stop_requested = True
