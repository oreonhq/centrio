import os
import subprocess

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage


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
        self.dual_boot = QCheckBox("Dual boot mode")
        self.preserve_efi = QCheckBox("Preserve existing EFI partition")
        self.page_layout.addWidget(QLabel("Target disk"))
        self.page_layout.addWidget(self.disk_combo)
        self.page_layout.addWidget(QLabel("Filesystem"))
        self.page_layout.addWidget(self.fs_combo)
        self.page_layout.addWidget(self.dual_boot)
        self.page_layout.addWidget(self.preserve_efi)
        btn = QPushButton("Apply Storage Settings")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

    def refresh_for_network(self):
        return

    def _list_disks(self):
        try:
            r = subprocess.run(
                ["lsblk", "-dn", "-o", "PATH,TYPE"],
                capture_output=True,
                text=True,
                timeout=8,
                check=True,
            )
            disks = []
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == "disk":
                    disks.append(parts[0])
            return disks or ["/dev/sda"]
        except Exception:
            return ["/dev/sda"]

    def apply_settings_and_return(self, _button=None):
        primary_disk = self.disk_combo.currentText().strip()
        if not primary_disk:
            self.show_toast("Please select a disk.")
            return
        fs = self.fs_combo.currentText().strip() or "ext4"
        dual = self.dual_boot.isChecked()
        preserve = self.preserve_efi.isChecked()
        partition_prefix = "p" if "nvme" in primary_disk else ""
        efi_part = f"{primary_disk}{partition_prefix}1"
        root_part = f"{primary_disk}{partition_prefix}2"

        commands = []
        if not (dual and preserve):
            commands.extend(
                [
                    ["wipefs", "-af", primary_disk],
                    ["parted", "-s", primary_disk, "mklabel", "gpt"],
                    ["parted", "-s", primary_disk, "mkpart", "ESP", "fat32", "1MiB", "513MiB"],
                    ["parted", "-s", primary_disk, "set", "1", "esp", "on"],
                    ["parted", "-s", primary_disk, "mkpart", "root", fs, "513MiB", "100%"],
                    ["mkfs.fat", "-F32", efi_part],
                    [f"mkfs.{fs}", "-F", root_part] if fs in ("ext4", "xfs") else ["mkfs.btrfs", "-f", root_part],
                ]
            )

        parts = [{"device": root_part, "mountpoint": "/", "fstype": fs}]
        if os.path.exists("/sys/firmware/efi"):
            parts.insert(
                0,
                {
                    "device": efi_part,
                    "mountpoint": "/boot/efi",
                    "fstype": "vfat",
                },
            )

        config_values = {
            "method": "dual_boot" if dual else "normal",
            "target_disks": [primary_disk],
            "filesystem": fs,
            "btrfs_subvolumes": fs == "btrfs",
            "dual_boot": dual,
            "preserve_efi": preserve,
            "selected_efi_partition": efi_part if preserve else None,
            "custom_format": False,
            "commands": commands,
            "partitions": parts,
        }
        self.mark_complete_and_return(config_values=config_values)
