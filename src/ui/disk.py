import json
import os
import re
import subprocess

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage


def _partition_prefix(disk_path):
    if "nvme" in disk_path or "mmcblk" in disk_path:
        return "p"
    return ""


def get_free_space_region(disk_path):
    """Largest free region on disk as (start, end) parted strings, or None."""
    if not disk_path or not os.path.exists(disk_path):
        return None
    try:
        r = subprocess.run(
            ["parted", "-s", disk_path, "unit", "MiB", "print", "free"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            return None
        best_start, best_end, best_size_mb = None, None, 0
        for line in (r.stdout or "").splitlines():
            if "Free Space" not in line and "free" not in line.lower():
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            start_s, end_s, size_s = parts[0], parts[1], parts[2]
            try:
                size_u = size_s.upper()
                num_s = (
                    size_u.replace("GIB", "")
                    .replace("GB", "")
                    .replace("MIB", "")
                    .replace("MB", "")
                    .replace("KB", "")
                    .replace("B", "")
                    .strip()
                )
                num = float(num_s) if num_s else 0.0
                if "GIB" in size_u or "GB" in size_u:
                    num *= 1024
                # Need room for a usable root (>= ~8 GiB)
                if num > best_size_mb and num >= 8192:
                    best_start, best_end, best_size_mb = start_s, end_s, num
            except (ValueError, IndexError):
                continue
        if best_start and best_end:
            return (best_start, best_end)
        return None
    except Exception:
        return None


def get_next_partition_device(disk_path):
    """Next partition path to create on disk (e.g. /dev/sda3, /dev/nvme0n1p4)."""
    if not disk_path:
        return None
    try:
        r = subprocess.run(
            ["lsblk", "-n", "-o", "NAME", "-l", disk_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        max_num = 0
        base = disk_path.rsplit("/", 1)[-1]
        for line in (r.stdout or "").splitlines():
            name = line.strip()
            if not name or name == base:
                continue
            if not name.startswith(base):
                continue
            suffix = name[len(base) :].lstrip("p")
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
        next_num = max_num + 1
        return f"{disk_path}{_partition_prefix(disk_path)}{next_num}"
    except Exception:
        return None


def detect_existing_efi_partitions(disk_path=None):
    """Detect EFI system partitions, optionally limited to one disk."""
    efi_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    efi_partitions = []
    seen = set()

    def _add(path, size=None, fstype="vfat"):
        if not path or path in seen:
            return
        if disk_path:
            # Keep only partitions belonging to the selected disk
            if not path.startswith(disk_path):
                return
        seen.add(path)
        efi_partitions.append({"path": path, "size": size, "fstype": fstype or "vfat"})

    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/boot/efi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            _add(r.stdout.strip())
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "PATH,FSTYPE,PARTTYPE,SIZE,PARTFLAGS"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        data = json.loads(r.stdout or "{}")

        def scan(device):
            path = device.get("path")
            fstype = (device.get("fstype") or "").lower()
            parttype = (device.get("parttype") or "").lower()
            flags = (device.get("partflags") or "").lower()
            size = device.get("size")
            is_efi = False
            if fstype in ("vfat", "fat32", "fat"):
                if parttype == efi_guid or "esp" in flags or "boot" in flags:
                    is_efi = True
            if is_efi:
                _add(path, size=size, fstype=fstype or "vfat")
            for child in device.get("children") or []:
                scan(child)

        for device in data.get("blockdevices") or []:
            scan(device)
    except Exception as e:
        print(f"Warning: Failed to detect EFI partitions: {e}")

    return efi_partitions


class DiskPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Disk Settings",
            subtitle="Disk selection and partitioning method",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        self.disks = self._list_disks()
        self.disk_combo = QComboBox()
        for d in self.disks:
            self.disk_combo.addItem(d)
        self.fs_combo = QComboBox()
        self.fs_combo.addItems(["ext4", "xfs", "btrfs"])
        self.dual_boot = QCheckBox("Dual boot mode (use free space, keep other OS)")
        self.preserve_efi = QCheckBox("Preserve existing EFI partition")
        self.preserve_efi.setChecked(True)
        self.efi_combo = QComboBox()
        self.efi_label = QLabel("Existing EFI partition")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("pageSubtitle")

        self.page_layout.addWidget(QLabel("Target disk"))
        self.page_layout.addWidget(self.disk_combo)
        self.page_layout.addWidget(QLabel("Filesystem"))
        self.page_layout.addWidget(self.fs_combo)
        self.page_layout.addWidget(self.dual_boot)
        self.page_layout.addWidget(self.preserve_efi)
        self.page_layout.addWidget(self.efi_label)
        self.page_layout.addWidget(self.efi_combo)
        self.page_layout.addWidget(self.status_label)

        btn = QPushButton("Apply Storage Settings")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

        self.dual_boot.toggled.connect(self._on_dual_boot_toggled)
        self.preserve_efi.toggled.connect(self._refresh_dual_boot_ui)
        self.disk_combo.currentTextChanged.connect(self._refresh_dual_boot_ui)
        self._on_dual_boot_toggled(self.dual_boot.isChecked())

    def refresh_for_network(self):
        return

    def _list_disks(self):
        try:
            r = subprocess.run(
                ["lsblk", "-J", "-b", "-d", "-o", "PATH,NAME,TYPE,SIZE,RM,RO"],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            payload = json.loads(r.stdout) if r.stdout else {}
            disks = []
            min_size_bytes = 8 * 1024 * 1024 * 1024
            skip_prefixes = ("loop", "zram", "ram", "dm-", "sr", "fd", "md")
            for dev in payload.get("blockdevices", []):
                if str(dev.get("type", "")).strip() != "disk":
                    continue
                if int(dev.get("ro", 0) or 0) != 0:
                    continue
                if int(dev.get("rm", 0) or 0) != 0:
                    continue
                name = str(dev.get("name", "")).strip()
                if not name or name.startswith(skip_prefixes):
                    continue
                size = int(dev.get("size", 0) or 0)
                if size < min_size_bytes:
                    continue
                path = str(dev.get("path", "")).strip()
                if path and path.startswith("/dev/"):
                    disks.append(path)
            return disks or ["/dev/sda"]
        except Exception:
            return ["/dev/sda"]

    def _on_dual_boot_toggled(self, checked):
        if checked:
            self.preserve_efi.setChecked(True)
        self._refresh_dual_boot_ui()

    def _refresh_dual_boot_ui(self, *_args):
        dual = self.dual_boot.isChecked()
        self.preserve_efi.setEnabled(dual)
        self.efi_label.setVisible(dual and self.preserve_efi.isChecked())
        self.efi_combo.setVisible(dual and self.preserve_efi.isChecked())

        if not dual:
            self.status_label.setText("Clean install will wipe the selected disk.")
            return

        disk = self.disk_combo.currentText().strip()
        free = get_free_space_region(disk) if disk else None
        efi_list = detect_existing_efi_partitions(disk) if disk else []
        self.efi_combo.clear()
        for e in efi_list:
            self.efi_combo.addItem(e["path"])

        msgs = []
        if not free:
            msgs.append("No usable free space (>= 8 GiB). Shrink a partition first.")
        else:
            msgs.append(f"Free space region: {free[0]} - {free[1]}")
        if self.preserve_efi.isChecked() and not efi_list:
            msgs.append("No EFI partition found on this disk.")
        elif efi_list:
            msgs.append(f"Found {len(efi_list)} EFI partition(s).")
        self.status_label.setText(" ".join(msgs))

    def apply_settings_and_return(self, _button=None):
        primary_disk = self.disk_combo.currentText().strip()
        if not primary_disk:
            self.show_toast("Please select a disk.")
            return
        fs = self.fs_combo.currentText().strip() or "ext4"
        dual = self.dual_boot.isChecked()
        # Dual boot always preserves the existing ESP. Wiping it kills the other OS.
        if dual:
            self.preserve_efi.setChecked(True)
        preserve = dual and self.preserve_efi.isChecked()
        is_uefi = os.path.exists("/sys/firmware/efi")
        prefix = _partition_prefix(primary_disk)

        commands = []
        partitions = []
        selected_efi = None
        root_part = None

        if dual:
            if not is_uefi:
                self.show_toast("Dual boot currently requires UEFI firmware.")
                return
            region = get_free_space_region(primary_disk)
            if not region:
                self.show_toast(
                    "Dual boot needs unallocated space (>= 8 GiB) on the selected disk."
                )
                return
            efi_list = detect_existing_efi_partitions(primary_disk)
            selected_efi = self.efi_combo.currentText().strip() or (
                efi_list[0]["path"] if efi_list else None
            )
            if not selected_efi:
                self.show_toast("No existing EFI partition found to preserve.")
                return
            root_part = get_next_partition_device(primary_disk)
            if not root_part:
                self.show_toast("Could not determine the next partition device.")
                return

            start, end = region
            commands.extend(
                [
                    [
                        "parted",
                        "-s",
                        primary_disk,
                        "mkpart",
                        "Linux filesystem",
                        fs,
                        start,
                        end,
                    ],
                    ["partprobe", primary_disk],
                    ["udevadm", "settle"],
                ]
            )
            if fs in ("ext4", "xfs"):
                commands.append([f"mkfs.{fs}", "-F", root_part])
            else:
                commands.append(["mkfs.btrfs", "-f", root_part])

            partitions.append(
                {"device": selected_efi, "mountpoint": "/boot/efi", "fstype": "vfat"}
            )
            partitions.append({"device": root_part, "mountpoint": "/", "fstype": fs})
            preserve = True
        else:
            efi_part = f"{primary_disk}{prefix}1"
            root_part = f"{primary_disk}{prefix}2"
            commands.extend(
                [
                    ["wipefs", "-af", primary_disk],
                    ["parted", "-s", primary_disk, "mklabel", "gpt"],
                ]
            )
            if is_uefi:
                commands.extend(
                    [
                        [
                            "parted",
                            "-s",
                            primary_disk,
                            "mkpart",
                            "ESP",
                            "fat32",
                            "1MiB",
                            "513MiB",
                        ],
                        ["parted", "-s", primary_disk, "set", "1", "esp", "on"],
                        [
                            "parted",
                            "-s",
                            primary_disk,
                            "mkpart",
                            "root",
                            fs,
                            "513MiB",
                            "100%",
                        ],
                        ["partprobe", primary_disk],
                        ["udevadm", "settle"],
                        ["mkfs.fat", "-F32", efi_part],
                    ]
                )
                partitions.append(
                    {"device": efi_part, "mountpoint": "/boot/efi", "fstype": "vfat"}
                )
            else:
                commands.extend(
                    [
                        [
                            "parted",
                            "-s",
                            primary_disk,
                            "mkpart",
                            "BIOS boot",
                            "",
                            "1MiB",
                            "3MiB",
                        ],
                        ["parted", "-s", primary_disk, "set", "1", "bios_grub", "on"],
                        [
                            "parted",
                            "-s",
                            primary_disk,
                            "mkpart",
                            "root",
                            fs,
                            "3MiB",
                            "100%",
                        ],
                        ["partprobe", primary_disk],
                        ["udevadm", "settle"],
                    ]
                )
            if fs in ("ext4", "xfs"):
                commands.append([f"mkfs.{fs}", "-F", root_part])
            else:
                commands.append(["mkfs.btrfs", "-f", root_part])
            partitions.append({"device": root_part, "mountpoint": "/", "fstype": fs})

        config_values = {
            "method": "dual_boot" if dual else "normal",
            "target_disks": [primary_disk],
            "filesystem": fs,
            "btrfs_subvolumes": fs == "btrfs",
            "dual_boot": dual,
            "preserve_efi": preserve,
            "selected_efi_partition": selected_efi if preserve else None,
            "custom_format": False,
            "commands": commands,
            "partitions": partitions,
        }
        self.mark_complete_and_return(config_values=config_values)
