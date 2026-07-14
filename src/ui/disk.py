import json
import os
import re
import subprocess

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage


LVM_VG = "oreon"
LVM_POOL = "pool"
LVM_ROOT = "root"
LVM_HOME = "home"
ESP_END_MIB = 513
BOOT_SIZE_MIB = 1024


def _partition_prefix(disk_path):
    if "nvme" in disk_path or "mmcblk" in disk_path:
        return "p"
    return ""


def _part(disk_path, num):
    return f"{disk_path}{_partition_prefix(disk_path)}{num}"


def _disk_size_mib(disk_path):
    try:
        r = subprocess.run(
            ["lsblk", "-b", "-d", "-n", "-o", "SIZE", disk_path],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        size_b = int((r.stdout or "0").strip() or "0")
        return max(0, size_b // (1024 * 1024))
    except Exception:
        return 0


def _parse_mib(token):
    """Parse parted size tokens like 100000MiB / 50GiB into MiB float."""
    if token is None:
        return None
    s = str(token).strip().upper().replace(",", "")
    try:
        if s.endswith("GIB") or s.endswith("GB"):
            return float(re.sub(r"[^0-9.]", "", s)) * 1024
        if s.endswith("MIB") or s.endswith("MB"):
            return float(re.sub(r"[^0-9.]", "", s))
        if s.endswith("KIB") or s.endswith("KB"):
            return float(re.sub(r"[^0-9.]", "", s)) / 1024
        if s.endswith("%"):
            return None
        return float(re.sub(r"[^0-9.]", "", s))
    except ValueError:
        return None


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
            num = _parse_mib(size_s) or 0
            # 1G /boot + ~8G root
            if num > best_size_mb and num >= 9216:
                best_start, best_end, best_size_mb = start_s, end_s, num
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
        if disk_path and not path.startswith(disk_path):
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


def _mkfs_cmd(fs, device):
    if fs == "xfs":
        return ["mkfs.xfs", "-f", device]
    if fs == "btrfs":
        return ["mkfs.btrfs", "-f", device]
    return ["mkfs.ext4", "-F", device]


def _lvm_thin_commands(lvm_part, root_fs, separate_home, usable_mib):
    """PV -> VG -> thin pool -> thin LV(s) for / and optional /home."""
    cmds = [
        ["pvcreate", "-ff", "-y", lvm_part],
        ["vgcreate", "-y", LVM_VG, lvm_part],
        ["lvcreate", "-y", "-l", "100%FREE", "--thinpool", LVM_POOL, LVM_VG],
    ]
    # Virtual sizes for thin LVs (MiB). Keep some headroom under the pool.
    pool_virt = max(8192, int(usable_mib) - 256)
    root_dev = f"/dev/{LVM_VG}/{LVM_ROOT}"
    if separate_home:
        root_v = max(40960, int(pool_virt * 0.4))
        if root_v > pool_virt - 2048:
            root_v = max(8192, pool_virt // 2)
        home_v = max(2048, pool_virt - root_v)
        cmds.append(
            [
                "lvcreate",
                "-y",
                "-V",
                f"{root_v}M",
                "--thin",
                f"{LVM_VG}/{LVM_POOL}",
                "-n",
                LVM_ROOT,
            ]
        )
        cmds.append(
            [
                "lvcreate",
                "-y",
                "-V",
                f"{home_v}M",
                "--thin",
                f"{LVM_VG}/{LVM_POOL}",
                "-n",
                LVM_HOME,
            ]
        )
        cmds.append(_mkfs_cmd(root_fs, root_dev))
        cmds.append(_mkfs_cmd(root_fs, f"/dev/{LVM_VG}/{LVM_HOME}"))
    else:
        cmds.append(
            [
                "lvcreate",
                "-y",
                "-V",
                f"{pool_virt}M",
                "--thin",
                f"{LVM_VG}/{LVM_POOL}",
                "-n",
                LVM_ROOT,
            ]
        )
        cmds.append(_mkfs_cmd(root_fs, root_dev))
    return cmds


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
        # Root/home on thin LVs; /boot stays ext4 on a normal partition
        self.fs_combo.addItems(["ext4", "xfs", "btrfs"])
        self.dual_boot = QCheckBox("Dual boot mode (use free space, keep other OS)")
        self.preserve_efi = QCheckBox("Preserve existing EFI partition")
        self.preserve_efi.setChecked(True)
        self.separate_home = QCheckBox("Separate /home thin LV")
        self.efi_combo = QComboBox()
        self.efi_label = QLabel("Existing EFI partition")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("pageSubtitle")

        self.page_layout.addWidget(QLabel("Target disk"))
        self.page_layout.addWidget(self.disk_combo)
        self.page_layout.addWidget(QLabel("Root filesystem (thin LV)"))
        self.page_layout.addWidget(self.fs_combo)
        self.page_layout.addWidget(self.dual_boot)
        self.page_layout.addWidget(self.preserve_efi)
        self.page_layout.addWidget(self.efi_label)
        self.page_layout.addWidget(self.efi_combo)
        self.page_layout.addWidget(self.separate_home)
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
            self.status_label.setText(
                "Clean install layout: ESP (UEFI) + 1G ext4 /boot + LVM thin pool for / "
                "(optional /home). /boot is a normal partition, not thin."
            )
            return

        disk = self.disk_combo.currentText().strip()
        free = get_free_space_region(disk) if disk else None
        efi_list = detect_existing_efi_partitions(disk) if disk else []
        self.efi_combo.clear()
        for e in efi_list:
            self.efi_combo.addItem(e["path"])

        msgs = []
        if not free:
            msgs.append("No usable free space (>= 9 GiB). Shrink a partition first.")
        else:
            msgs.append(f"Free space region: {free[0]} - {free[1]}")
        if self.preserve_efi.isChecked() and not efi_list:
            msgs.append("No EFI partition found on this disk.")
        elif efi_list:
            msgs.append(f"Found {len(efi_list)} EFI partition(s).")
        msgs.append("Dual boot uses 1G /boot + LVM thin / in free space.")
        self.status_label.setText(" ".join(msgs))

    def apply_settings_and_return(self, _button=None):
        primary_disk = self.disk_combo.currentText().strip()
        if not primary_disk:
            self.show_toast("Please select a disk.")
            return
        fs = self.fs_combo.currentText().strip() or "ext4"
        dual = self.dual_boot.isChecked()
        if dual:
            self.preserve_efi.setChecked(True)
        preserve = bool(dual)
        separate_home = self.separate_home.isChecked()
        is_uefi = os.path.exists("/sys/firmware/efi")

        commands = []
        partitions = []
        selected_efi = None
        boot_part = None
        lvm_part = None

        if dual:
            if not is_uefi:
                self.show_toast("Dual boot currently requires UEFI firmware.")
                return
            region = get_free_space_region(primary_disk)
            if not region:
                self.show_toast(
                    "Dual boot needs unallocated space (>= 9 GiB) on the selected disk."
                )
                return
            efi_list = detect_existing_efi_partitions(primary_disk)
            selected_efi = self.efi_combo.currentText().strip() or (
                efi_list[0]["path"] if efi_list else None
            )
            if not selected_efi:
                self.show_toast("No existing EFI partition found to preserve.")
                return
            boot_part = get_next_partition_device(primary_disk)
            if not boot_part:
                self.show_toast("Could not determine the next partition device.")
                return
            # Predict LVM part number as boot_part + 1
            boot_num = int(re.search(r"(\d+)$", boot_part).group(1))
            lvm_part = _part(primary_disk, boot_num + 1)

            start_mib = _parse_mib(region[0])
            end_mib = _parse_mib(region[1])
            if start_mib is None or end_mib is None or (end_mib - start_mib) < 9216:
                self.show_toast("Free space is too small for /boot + LVM root.")
                return
            boot_end_mib = start_mib + BOOT_SIZE_MIB
            commands.extend(
                [
                    [
                        "parted",
                        "-s",
                        primary_disk,
                        "mkpart",
                        "boot",
                        "ext4",
                        f"{int(start_mib)}MiB",
                        f"{int(boot_end_mib)}MiB",
                    ],
                    [
                        "parted",
                        "-s",
                        primary_disk,
                        "mkpart",
                        "lvm",
                        "ext4",
                        f"{int(boot_end_mib)}MiB",
                        region[1],
                    ],
                    ["parted", "-s", primary_disk, "set", str(boot_num + 1), "lvm", "on"],
                    ["partprobe", primary_disk],
                    ["udevadm", "settle"],
                    ["mkfs.ext4", "-F", boot_part],
                ]
            )
            usable = int(end_mib - boot_end_mib)
            commands.extend(
                _lvm_thin_commands(lvm_part, fs, separate_home, usable)
            )
            partitions.append(
                {"device": selected_efi, "mountpoint": "/boot/efi", "fstype": "vfat"}
            )
            partitions.append(
                {"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"}
            )
            partitions.append(
                {
                    "device": f"/dev/{LVM_VG}/{LVM_ROOT}",
                    "mountpoint": "/",
                    "fstype": fs,
                }
            )
            if separate_home:
                partitions.append(
                    {
                        "device": f"/dev/{LVM_VG}/{LVM_HOME}",
                        "mountpoint": "/home",
                        "fstype": fs,
                    }
                )
        else:
            disk_mib = _disk_size_mib(primary_disk)
            if disk_mib < 10240:
                self.show_toast("Disk is too small (need at least ~10 GiB).")
                return

            commands.extend(
                [
                    ["wipefs", "-af", primary_disk],
                    ["parted", "-s", primary_disk, "mklabel", "gpt"],
                ]
            )

            part_num = 1
            if is_uefi:
                efi_part = _part(primary_disk, part_num)
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
                            f"{ESP_END_MIB}MiB",
                        ],
                        ["parted", "-s", primary_disk, "set", "1", "esp", "on"],
                    ]
                )
                partitions.append(
                    {"device": efi_part, "mountpoint": "/boot/efi", "fstype": "vfat"}
                )
                boot_start = ESP_END_MIB
                part_num = 2
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
                    ]
                )
                boot_start = 3
                part_num = 2

            boot_part = _part(primary_disk, part_num)
            boot_end = boot_start + BOOT_SIZE_MIB
            lvm_part_num = part_num + 1
            lvm_part = _part(primary_disk, lvm_part_num)

            commands.extend(
                [
                    [
                        "parted",
                        "-s",
                        primary_disk,
                        "mkpart",
                        "boot",
                        "ext4",
                        f"{boot_start}MiB",
                        f"{boot_end}MiB",
                    ],
                    [
                        "parted",
                        "-s",
                        primary_disk,
                        "mkpart",
                        "lvm",
                        "ext4",
                        f"{boot_end}MiB",
                        "100%",
                    ],
                    ["parted", "-s", primary_disk, "set", str(lvm_part_num), "lvm", "on"],
                    ["partprobe", primary_disk],
                    ["udevadm", "settle"],
                ]
            )
            if is_uefi:
                commands.append(["mkfs.fat", "-F32", _part(primary_disk, 1)])
            commands.append(["mkfs.ext4", "-F", boot_part])

            usable = max(8192, disk_mib - boot_end - 64)
            commands.extend(_lvm_thin_commands(lvm_part, fs, separate_home, usable))

            partitions.append(
                {"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"}
            )
            partitions.append(
                {
                    "device": f"/dev/{LVM_VG}/{LVM_ROOT}",
                    "mountpoint": "/",
                    "fstype": fs,
                }
            )
            if separate_home:
                partitions.append(
                    {
                        "device": f"/dev/{LVM_VG}/{LVM_HOME}",
                        "mountpoint": "/home",
                        "fstype": fs,
                    }
                )

        config_values = {
            "method": "dual_boot" if dual else "normal",
            "target_disks": [primary_disk],
            "filesystem": fs,
            "btrfs_subvolumes": False,
            "dual_boot": dual,
            "preserve_efi": preserve,
            "selected_efi_partition": selected_efi if preserve else None,
            "custom_format": False,
            "lvm_thin": True,
            "lvm_vg": LVM_VG,
            "lvm_pool": LVM_POOL,
            "lvm_root_lv": LVM_ROOT,
            "lvm_home_lv": LVM_HOME if separate_home else None,
            "separate_boot": True,
            "separate_home": separate_home,
            "commands": commands,
            "partitions": partitions,
        }
        self.mark_complete_and_return(config_values=config_values)
