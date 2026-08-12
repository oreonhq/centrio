import threading
import os
import subprocess
import time

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

    _CORE_PACKAGES = frozenset([
        "@core", "kernel", "grub2-efi-x64", "grub2-efi-x64-modules", "grub2-efi-aa64", "grub2-efi-aa64-modules",
        "grub2-pc", "grub2-common", "grub2-tools", "shim-x64", "shim-aa64", "shim", "efibootmgr",
        "efitools",
        "flatpak", "xdg-desktop-portal", "xdg-desktop-portal-gtk", "centrio-installer",
    ])

    def _install_additional_packages(self, config_data, offline_install):
        payload_cfg = config_data.get("payload", {}) if isinstance(config_data, dict) else {}
        disk_cfg = config_data.get("disk", {}) if isinstance(config_data, dict) else {}
        btrfs_subvolumes = bool(disk_cfg.get("btrfs_subvolumes", False))

        packages = payload_cfg.get("packages", [])
        repositories = payload_cfg.get("repositories", [])
        flatpak_enabled = payload_cfg.get("flatpak_enabled", False)
        flatpak_packages = payload_cfg.get("flatpak_packages", [])
        nvidia_drivers = bool(payload_cfg.get("nvidia_drivers", False))

        extra_packages = [p for p in packages if p not in self._CORE_PACKAGES]
        snapper_packages = (
            ["snapper", "python3-dnf-plugin-snapper", "grub-btrfs", "inotify-tools"]
            if btrfs_subvolumes else []
        )
        has_extra_work = bool(
            extra_packages or repositories or flatpak_enabled or flatpak_packages
            or btrfs_subvolumes or nvidia_drivers
        )
        if not has_extra_work:
            print("No additional packages selected - base system only")
            return True, ""

        needs_network = bool(
            extra_packages or repositories or flatpak_enabled or flatpak_packages
            or (nvidia_drivers and not offline_install)
        )
        if needs_network and not offline_install:
            self._update_progress_text("Refreshing network...", 0.74)
            backend.restart_network_manager()
            self._update_progress_text("Checking network connectivity...", 0.74)
            network_ok = False
            for _ in range(8):
                if backend.check_network_connectivity():
                    network_ok = True
                    break
                time.sleep(2)
            if not network_ok:
                raise RuntimeError(
                    "Additional software was selected, but no internet connection is available. "
                    "Connect to Wi-Fi/Ethernet and retry, or choose Continue without network "
                    "to install base system only."
                )
        elif needs_network and offline_install:
            raise RuntimeError(
                "Additional software was selected, but this install is offline. "
                "Connect to the network or deselect extra packages and NVIDIA drivers."
            )

        package_config = {
            "packages": extra_packages + snapper_packages,
            "repositories": repositories,
            "flatpak_enabled": flatpak_enabled,
            "flatpak_packages": flatpak_packages,
            "nvidia_drivers": nvidia_drivers,
            "server_install": bool(payload_cfg.get("server_install", False)),
            "minimal_install": False,
            "keep_cache": payload_cfg.get("keep_cache", True),
            "btrfs_subvolumes": btrfs_subvolumes,
            "offline_install": offline_install,
        }
        return backend.install_packages_on_live_copy(
            self.target_root,
            package_config,
            progress_callback=self._scaled_progress_callback(0.74, 0.82),
        )

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
            offline_install = backend.install_skipped_network(config_data)
            disk_config = config_data.get("disk", {})
            commands = disk_config.get("commands", [])
            partitions = disk_config.get("partitions", [])
            primary_disk = (disk_config.get("target_disks") or [None])[0]
            expect_parts = int(disk_config.get("expect_partitions") or 0)
            storage_cb = self._scaled_progress_callback(0.02, 0.2)

            if primary_disk and commands:
                stop_ok, stop_err = backend._stop_service("udisks2.service")
                if not stop_ok:
                    print(f"Warning: could not stop udisks2 (continuing): {stop_err}")
                else:
                    print("Stopped udisks2 temporarily for storage setup.")
                    udisks_stopped_for_storage = True
                rel_ok, rel_err, teardown_vgs = backend.release_disk_for_install(
                    primary_disk, storage_cb
                )
                if not rel_ok:
                    raise RuntimeError(
                        f"Could not release target disk. Details: {rel_err}"
                    )
                if teardown_vgs:
                    disk_config["lvm_teardown_vgs"] = teardown_vgs
                    print(f"LVM torn down on {primary_disk}: {teardown_vgs}")
                backend.forget_kernel_partitions(primary_disk, storage_cb)
                backend._start_service("systemd-udevd.service")
                try:
                    subprocess.run(["udevadm", "settle"], check=False, timeout=15)
                except Exception:
                    pass
                from ui.disk import refresh_disk_config_lvm

                refresh_disk_config_lvm(disk_config)
                commands = disk_config.get("commands", [])
                partitions = disk_config.get("partitions", [])
                from ui.disk import SCHEME_BTRFS, SCHEME_THIN, _vg_name_blocked
                import storage_layout

                scheme = disk_config.get("storage_scheme") or (
                    SCHEME_THIN if disk_config.get("lvm_thin") else None
                )
                vg_now = disk_config.get("lvm_vg")
                print(
                    f"Install scheme={scheme} VG={vg_now} PV={disk_config.get('lvm_pv')}"
                )
                if scheme != SCHEME_BTRFS:
                    if not vg_now:
                        raise RuntimeError("No LVM VG name selected for install")
                    if _vg_name_blocked(vg_now):
                        raise RuntimeError(
                            f"Live session already owns LVM/DM name '{vg_now}'. "
                            "Installer refused to create a colliding volume group."
                        )

            self._update_progress_text("Preparing storage...", 0.02)
            for idx, cmd in enumerate(commands):
                cmd_timeout = 120
                cmd_bin = None
                if cmd:
                    for t in cmd:
                        if (
                            t
                            and not str(t).startswith("-")
                            and "=" not in str(t)
                            and t not in ("env", "sudo", "bash")
                        ):
                            cmd_bin = t
                            break
                    if cmd_bin is None:
                        cmd_bin = cmd[0]
                if cmd and cmd[0] == "bash" and any("sfdisk" in str(t) for t in cmd):
                    cmd_bin = "sfdisk"
                if cmd and cmd[0] == "udevadm" and "settle" in cmd:
                    cmd_timeout = 45
                    if not any(
                        str(t).startswith("--timeout") or str(t).startswith("-t")
                        for t in cmd
                    ):
                        cmd = ["udevadm", "settle", "--timeout=30"]

                ok, err, _ = backend._run_command(
                    cmd,
                    f"Storage step {idx + 1}",
                    progress_callback=storage_cb,
                    timeout=cmd_timeout,
                )
                if not ok:
                    if (
                        primary_disk
                        and expect_parts
                        and cmd_bin in ("sfdisk", "parted")
                        and backend.table_written_kernel_busy(err)
                    ):
                        vis_ok, vis_err = backend.ensure_partitions_visible(
                            primary_disk, expect_parts, storage_cb
                        )
                        if not vis_ok:
                            raise RuntimeError(vis_err or err)
                    elif (
                        primary_disk
                        and expect_parts
                        and cmd_bin == "partprobe"
                    ):
                        if not backend.partition_nodes_ready(
                            primary_disk, expect_parts
                        ):
                            vis_ok, vis_err = backend.ensure_partitions_visible(
                                primary_disk, expect_parts, storage_cb
                            )
                            if not vis_ok:
                                raise RuntimeError(err)
                    else:
                        raise RuntimeError(
                            err or f"Storage command failed: {' '.join(cmd)}"
                        )

            # Industry path: libblockdev (same stack as Anaconda/blivet)
            import storage_layout

            self._update_progress_text("Creating root storage...", 0.08)
            ok, err = storage_layout.apply_root_storage(disk_config, storage_cb)
            if not ok:
                raise RuntimeError(err or "Root storage setup failed")
            partitions = disk_config.get("partitions", [])

            if not backend.ensure_directory(self.target_root):
                raise RuntimeError(f"Could not create {self.target_root}")

            def _mount_sort_key(part):
                mp = part.get("mountpoint") or ""
                if mp == "/":
                    return (0, "")
                depth = len([p for p in mp.split("/") if p])
                return (depth, mp)

            for part in sorted(partitions, key=_mount_sort_key):
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

            fs_now = (disk_config.get("filesystem") or "ext4").lower()
            if fs_now != "ext4":
                rm_ok, rm_err = backend.remove_oreon_root_protection(
                    self.target_root,
                    progress_callback=self._scaled_progress_callback(0.75, 0.76),
                )
                if not rm_ok:
                    raise RuntimeError(rm_err or "Failed to remove oreon-root-protection")

            cfg_ok, cfg_err = backend.configure_system_in_container(
                self.target_root,
                config_data,
                progress_callback=self._scaled_progress_callback(0.76, 0.78),
            )
            if not cfg_ok:
                raise RuntimeError(cfg_err or "System configuration failed")

            pkg_ok, pkg_err = self._install_additional_packages(config_data, offline_install)
            if not pkg_ok:
                raise RuntimeError(pkg_err or "Additional package installation failed")

            cleanup_ok, cleanup_err = backend.remove_live_users_and_configure_oobe(
                self.target_root,
                install_user_created=False,
                install_username=None,
                progress_callback=self._scaled_progress_callback(0.82, 0.88),
                btrfs_subvolumes=bool(disk_config.get("btrfs_subvolumes", False)),
            )
            if not cleanup_ok:
                raise RuntimeError(cleanup_err or "Failed to remove live users")

            efi = None
            boot_part = None
            for part in disk_config.get("partitions", []):
                if part.get("mountpoint") == "/boot/efi":
                    efi = part.get("device")
                elif part.get("mountpoint") == "/boot":
                    boot_part = part.get("device")
            dual_boot = bool(disk_config.get("dual_boot", False))
            preserve_efi = bool(disk_config.get("preserve_efi", False))
            boot_ok, boot_err, _ = backend.install_bootloader_in_container(
                self.target_root,
                primary_disk,
                efi,
                progress_callback=self._scaled_progress_callback(0.87, 0.99),
                boot_partition_device=boot_part,
                offline_install=offline_install,
                dual_boot=dual_boot,
                preserve_efi=preserve_efi,
            )
            if not boot_ok:
                raise RuntimeError(boot_err or "Bootloader install failed")

            payload_cfg = config_data.get("payload", {}) if isinstance(config_data, dict) else {}
            disk_cfg = config_data.get("disk", {}) if isinstance(config_data, dict) else {}
            lvm_vg = disk_cfg.get("lvm_vg")
            lvm_root_lv = disk_cfg.get("lvm_root_lv") or "root"
            lvm_root = f"{lvm_vg}/{lvm_root_lv}" if lvm_vg else None
            post_ok, post_err = backend.setup_live_environment_post_copy(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.94, 0.995),
                server_install=bool(payload_cfg.get("server_install", False)),
                btrfs_subvolumes=bool(disk_cfg.get("btrfs_subvolumes", False)),
                offline_install=offline_install,
                lvm_root=lvm_root,
                separate_boot=bool(disk_cfg.get("separate_boot", False)),
            )
            if not post_ok:
                raise RuntimeError(post_err or "Post-copy system finalization failed")

            fstab_ok, fstab_err = backend.generate_fstab_for_target(
                self.target_root,
                progress_callback=self._scaled_progress_callback(0.995, 0.998),
            )
            if not fstab_ok:
                raise RuntimeError(fstab_err or "Failed to generate fstab")

            if not bool(payload_cfg.get("server_install", False)):
                ps_ok, ps_err = backend.configure_plasma_setup_oobe(
                    self.target_root,
                    progress_callback=self._scaled_progress_callback(0.997, 0.999),
                )
                if not ps_ok:
                    raise RuntimeError(ps_err or "Plasma Setup OOBE configuration failed")

            backend.remove_centrio_installer(offline_install=offline_install)
            self.signals.done.emit(True, "")
        except Exception as e:
            self.installation_error = str(e)
            self.signals.done.emit(False, self.installation_error)
        finally:
            if udisks_stopped_for_storage:
                backend._start_service("udisks2.service")

    def stop_installation(self):
        self.stop_requested = True
