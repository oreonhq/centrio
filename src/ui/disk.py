import hashlib
import json
import os
import re
import shlex
import subprocess

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage


LVM_VG_PREFERRED = "oreon"
LVM_POOL = "pool"
LVM_ROOT = "root"
LVM_HOME = "home"
ESP_END_MIB = 513
BOOT_SIZE_MIB = 1024

PART_TYPE_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
PART_TYPE_LVM = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
PART_TYPE_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
PART_TYPE_BIOS_BOOT = "21686148-6449-6E6F-744E-656564454649"

SCHEME_THIN = "lvm_thin"
SCHEME_LVM = "lvm"
SCHEME_BTRFS = "btrfs"


def _dm_leaf(name):
    return str(name).replace("-", "--")


def _mapper_path(vg_name, lv_name):
    return f"/dev/mapper/{_dm_leaf(vg_name)}-{_dm_leaf(lv_name)}"


def _lvm_dev(vg_name, lv_name):
    return _mapper_path(vg_name, lv_name)


def _is_live_install_env():
    try:
        with open("/proc/cmdline", encoding="utf-8") as f:
            cmdline = f.read().lower()
        if any(
            tok in cmdline
            for tok in (
                "rd.live",
                "root=live",
                " liveimg",
                " boot=live",
                "oreon.live",
            )
        ):
            return True
    except OSError:
        pass
    for path in (
        "/run/initramfs/live",
        "/run/live",
        "/lib/live/mount",
        "/.live",
    ):
        if os.path.exists(path):
            return True
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        fstype = (r.stdout or "").strip().lower()
        if fstype in ("overlay", "overlayfs"):
            return True
    except Exception:
        pass
    return False


def _disk_vg_suffix(disk_path):
    raw = disk_path or ""
    try:
        r = subprocess.run(
            ["lsblk", "-dn", "-o", "SERIAL,UUID,PKNAME", disk_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            raw = f"{disk_path}:{r.stdout.strip()}"
    except Exception:
        pass
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
    return re.sub(r"[^a-z0-9]", "", digest)[:4] or "disk"


def _partition_prefix(disk_path):
    if "nvme" in disk_path or "mmcblk" in disk_path:
        return "p"
    return ""


def _part(disk_path, num):
    return f"{disk_path}{_partition_prefix(disk_path)}{num}"


def _sfdisk_cmd(disk, script):
    return [
        "bash",
        "-c",
        f"sfdisk --wipe always --force {shlex.quote(disk)} <<'EOF'\n{script}EOF",
    ]


def _clean_install_sfdisk_script(is_uefi, root_part_type=PART_TYPE_LVM):
    root_name = "root" if root_part_type == PART_TYPE_LINUX else "lvm"
    if is_uefi:
        return (
            "label: gpt\n"
            f"name=ESP, start=1MiB, size=512MiB, type={PART_TYPE_ESP}\n"
            f"name=boot, start={ESP_END_MIB}MiB, size={BOOT_SIZE_MIB}MiB, "
            f"type={PART_TYPE_LINUX}\n"
            f"name={root_name}, start={ESP_END_MIB + BOOT_SIZE_MIB}MiB, "
            f"type={root_part_type}\n"
        )
    return (
        "label: gpt\n"
        f"name=biosboot, start=1MiB, size=2MiB, type={PART_TYPE_BIOS_BOOT}\n"
        f"name=boot, start=3MiB, size={BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
        f"name={root_name}, start={3 + BOOT_SIZE_MIB}MiB, "
        f"type={root_part_type}\n"
    )


def _dual_boot_sfdisk_append(disk, start_mib, boot_end_mib, end_mib, root_part_type=PART_TYPE_LVM):
    root_name = "root" if root_part_type == PART_TYPE_LINUX else "lvm"
    script = (
        f"name=boot, start={int(start_mib)}MiB, size={BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
        f"name={root_name}, start={int(boot_end_mib)}MiB, "
        f"size={int(end_mib - boot_end_mib)}MiB, "
        f"type={root_part_type}\n"
    )
    return [
        "bash",
        "-c",
        f"sfdisk --append --force {shlex.quote(disk)} <<'EOF'\n{script}EOF",
    ]


def _mapper_entry_to_vg(entry):
    if not entry or entry == "control" or "-" not in entry:
        return None
    return entry.split("-", 1)[0].replace("--", "-") or None


def _vg_from_source(src):
    if not src:
        return None
    src = src.strip()
    if src.startswith("/dev/mapper/"):
        return _mapper_entry_to_vg(src.rsplit("/", 1)[-1])
    if src.startswith("/dev/") and src.count("/") >= 3:
        # /dev/vgname/lvname
        parts = src.split("/")
        if len(parts) >= 4 and parts[2]:
            return parts[2]
    return None


def _existing_vg_names():
    names = set()
    try:
        r = subprocess.run(
            ["vgs", "-o", "name", "--noheadings", "--nolocking"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                n = line.strip()
                if n:
                    names.add(n)
    except Exception:
        pass
    try:
        for entry in os.listdir("/dev/mapper"):
            vg = _mapper_entry_to_vg(entry)
            if vg:
                names.add(vg)
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["dmsetup", "ls"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                name = (line.split() or [""])[0].strip()
                vg = _mapper_entry_to_vg(name)
                if vg:
                    names.add(vg)
    except Exception:
        pass
    for mp in ("/", "/home", "/boot", "/boot/efi"):
        try:
            r = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", mp],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                vg = _vg_from_source((r.stdout or "").strip())
                if vg:
                    names.add(vg)
        except Exception:
            pass
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                vg = _vg_from_source(parts[0])
                if vg:
                    names.add(vg)
    except OSError:
        pass
    return names


def _vg_name_blocked(name):
    if not name:
        return True
    if name in _existing_vg_names():
        return True
    if os.path.isdir(f"/dev/{name}"):
        return True
    prefix = f"{name}-"
    try:
        for entry in os.listdir("/dev/mapper"):
            if entry == name or entry.startswith(prefix):
                return True
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["dmsetup", "ls"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                entry = (line.split() or [""])[0].strip()
                if entry == name or entry.startswith(prefix):
                    return True
    except Exception:
        pass
    return False


def _pick_vg_name(preferred=None, disk_path=None):
    preferred = preferred or LVM_VG_PREFERRED
    existing = _existing_vg_names()
    candidates = []
    live = _is_live_install_env()
    if disk_path:
        candidates.append(f"{preferred}{_disk_vg_suffix(disk_path)}")
    if not live:
        candidates.insert(0, preferred)
    for n in range(0, 64):
        candidates.append(f"{preferred}{n}")
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if cand in existing or _vg_name_blocked(cand):
            continue
        if live and cand == preferred:
            continue
        if cand != preferred:
            print(
                f"VG '{preferred}' is not safe on this live system; using '{cand}'"
            )
        return cand
    raise RuntimeError(
        f"Could not find a free LVM VG name based on '{preferred}'"
    )


def rewrite_disk_config_vg(disk_config, new_vg):
    if not isinstance(disk_config, dict) or not new_vg:
        return disk_config
    old = disk_config.get("lvm_vg") or LVM_VG_PREFERRED
    disk_config["lvm_vg"] = new_vg
    if old == new_vg:
        return disk_config
    for part in disk_config.get("partitions") or []:
        dev = part.get("device")
        if not isinstance(dev, str):
            continue
        if f"/dev/{old}/" in dev:
            part["device"] = dev.replace(f"/dev/{old}/", f"/dev/{new_vg}/", 1)
        elif f"/dev/mapper/{_dm_leaf(old)}-" in dev:
            part["device"] = dev.replace(
                f"/dev/mapper/{_dm_leaf(old)}-",
                f"/dev/mapper/{_dm_leaf(new_vg)}-",
                1,
            )
    fixed = []
    for cmd in disk_config.get("commands") or []:
        if not isinstance(cmd, (list, tuple)):
            fixed.append(cmd)
            continue
        row = []
        for tok in cmd:
            if not isinstance(tok, str):
                row.append(tok)
                continue
            if tok == old:
                row.append(new_vg)
            else:
                row.append(
                    tok.replace(f"/dev/{old}/", f"/dev/{new_vg}/")
                    .replace(f"{old}/", f"{new_vg}/")
                    .replace(
                        f"/dev/mapper/{_dm_leaf(old)}-",
                        f"/dev/mapper/{_dm_leaf(new_vg)}-",
                    )
                )
        fixed.append(row)
    disk_config["commands"] = fixed
    return disk_config


def _guess_lvm_pv(disk_config):
    pv = disk_config.get("lvm_pv")
    if isinstance(pv, str) and pv.startswith("/dev/"):
        return pv
    disks = disk_config.get("target_disks") or []
    expect = int(disk_config.get("expect_partitions") or 3)
    if disks and expect:
        return _part(disks[0], expect)
    return None


def _is_root_storage_cmd(cmd, root_part, disks):
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return False
    joined = " ".join(str(t) for t in cmd)
    markers = (
        "pvcreate",
        "vgcreate",
        "lvcreate",
        "lvchange",
        "vgchange",
        "dm-thin-pool",
        "modprobe",
    )
    if any(m in joined for m in markers):
        return True
    if cmd[0] == "wipefs" and root_part in cmd and (not disks or disks[0] not in cmd):
        return True
    if root_part in joined and any(
        x in joined for x in ("mkfs.ext4", "mkfs.xfs", "mkfs.btrfs")
    ):
        return True
    return False


def refresh_disk_config_lvm(disk_config):
    if not isinstance(disk_config, dict):
        return disk_config
    root_part = _guess_lvm_pv(disk_config)
    if not root_part:
        raise RuntimeError("No root storage device in disk config")
    disk_config["lvm_pv"] = root_part
    disks = disk_config.get("target_disks") or []
    disk_path = disks[0] if disks else None
    fs = disk_config.get("filesystem") or "ext4"
    scheme = disk_config.get("storage_scheme") or storage_scheme_for_fs(fs)
    separate_home = bool(disk_config.get("separate_home")) and scheme != SCHEME_BTRFS
    disk_config["storage_scheme"] = scheme
    disk_config["lvm_thin"] = scheme == SCHEME_THIN
    disk_config["btrfs_subvolumes"] = scheme == SCHEME_BTRFS
    disk_config["separate_home"] = separate_home

    # Partition commands only. Root LV/FS is created by storage_layout/libblockdev.
    disk_config["commands"] = [
        list(cmd)
        for cmd in (disk_config.get("commands") or [])
        if not _is_root_storage_cmd(cmd, root_part, disks)
    ]

    if scheme in (SCHEME_THIN, SCHEME_LVM):
        vg_name = _pick_vg_name(LVM_VG_PREFERRED, disk_path=disk_path)
        if _vg_name_blocked(vg_name):
            raise RuntimeError(
                f"Refusing to create LVM VG '{vg_name}': name already in use on this live system"
            )
        disk_config["lvm_vg"] = vg_name
        disk_config["lvm_pool"] = LVM_POOL if scheme == SCHEME_THIN else None
        disk_config["lvm_root_lv"] = LVM_ROOT
        disk_config["lvm_home_lv"] = LVM_HOME if separate_home else None
        for part in disk_config.get("partitions") or []:
            mp = part.get("mountpoint")
            if mp == "/":
                part["device"] = _lvm_dev(vg_name, LVM_ROOT)
            elif mp == "/home" and separate_home:
                part["device"] = _lvm_dev(vg_name, LVM_HOME)
        print(f"Install scheme={scheme} VG={vg_name} PV={root_part}")
    else:
        disk_config["lvm_vg"] = None
        disk_config["lvm_pool"] = None
        disk_config["lvm_root_lv"] = None
        disk_config["lvm_home_lv"] = None
        for part in disk_config.get("partitions") or []:
            if part.get("mountpoint") == "/":
                part["device"] = root_part
        print(f"Install scheme={scheme} root={root_part}")
    return disk_config


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
            if num > best_size_mb and num >= 9216:
                best_start, best_end, best_size_mb = start_s, end_s, num
        if best_start and best_end:
            return (best_start, best_end)
        return None
    except Exception:
        return None


def get_next_partition_device(disk_path):
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


def storage_scheme_for_fs(fs):
    fs = (fs or "ext4").lower()
    if fs == "btrfs":
        return SCHEME_BTRFS
    if fs == "xfs":
        return SCHEME_LVM
    return SCHEME_THIN



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
        self.separate_home = QCheckBox("Separate /home LV")
        self.efi_combo = QComboBox()
        self.efi_label = QLabel("Existing EFI partition")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("pageSubtitle")

        self.page_layout.addWidget(QLabel("Target disk"))
        self.page_layout.addWidget(self.disk_combo)
        self.page_layout.addWidget(QLabel("Root filesystem"))
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
        self.fs_combo.currentTextChanged.connect(self._on_fs_changed)
        self._on_dual_boot_toggled(self.dual_boot.isChecked())
        self._on_fs_changed(self.fs_combo.currentText())

    def refresh_for_network(self):
        return

    def _on_fs_changed(self, *_args):
        fs = (self.fs_combo.currentText() or "ext4").strip().lower()
        scheme = storage_scheme_for_fs(fs)
        if scheme == SCHEME_BTRFS:
            self.separate_home.setChecked(False)
            self.separate_home.setEnabled(False)
            self.separate_home.setText("Separate /home (btrfs uses subvolumes)")
            self.status_label.setText(
                "btrfs: ESP + /boot + btrfs root with subvolumes (no LVM)."
            )
        elif scheme == SCHEME_LVM:
            self.separate_home.setEnabled(True)
            self.separate_home.setText("Separate /home LV")
            self.status_label.setText(
                "xfs: ESP + /boot + standard LVM (not thin)."
            )
        else:
            self.separate_home.setEnabled(True)
            self.separate_home.setText("Separate /home LV")
            self.status_label.setText(
                "ext4: ESP + /boot + thin LVM pool (oreon-root-protection)."
            )

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
        if not checked:
            self._on_fs_changed()

    def _refresh_dual_boot_ui(self, *_args):
        dual = self.dual_boot.isChecked()
        self.preserve_efi.setEnabled(dual)
        self.efi_label.setVisible(dual and self.preserve_efi.isChecked())
        self.efi_combo.setVisible(dual and self.preserve_efi.isChecked())

        if not dual:
            self.status_label.setText("")
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
            msgs.append(f"Free space: {free[0]} - {free[1]}")
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
        scheme = storage_scheme_for_fs(fs)
        dual = self.dual_boot.isChecked()
        if dual:
            self.preserve_efi.setChecked(True)
        preserve = bool(dual)
        separate_home = self.separate_home.isChecked() and scheme != SCHEME_BTRFS
        is_uefi = os.path.exists("/sys/firmware/efi")
        root_part_type = (
            PART_TYPE_LINUX if scheme == SCHEME_BTRFS else PART_TYPE_LVM
        )

        commands = []
        partitions = []
        selected_efi = None
        boot_part = None
        root_part = None
        vg_name = None
        if scheme in (SCHEME_THIN, SCHEME_LVM):
            vg_name = _pick_vg_name(LVM_VG_PREFERRED, disk_path=primary_disk)

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
            boot_num = int(re.search(r"(\d+)$", boot_part).group(1))
            root_part = _part(primary_disk, boot_num + 1)

            start_mib = _parse_mib(region[0])
            end_mib = _parse_mib(region[1])
            if start_mib is None or end_mib is None or (end_mib - start_mib) < 9216:
                self.show_toast("Free space is too small for /boot + root.")
                return
            boot_end_mib = start_mib + BOOT_SIZE_MIB
            expect_parts = boot_num + 1
            commands.extend(
                [
                    _dual_boot_sfdisk_append(
                        primary_disk,
                        start_mib,
                        boot_end_mib,
                        end_mib,
                        root_part_type=root_part_type,
                    ),
                    ["partprobe", primary_disk],
                    ["udevadm", "settle", "--timeout=30"],
                    ["mkfs.ext4", "-F", boot_part],
                ]
            )
            usable = int(end_mib - boot_end_mib)
            partitions.append(
                {"device": selected_efi, "mountpoint": "/boot/efi", "fstype": "vfat"}
            )
            partitions.append(
                {"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"}
            )
            if scheme == SCHEME_BTRFS:
                partitions.append(
                    {"device": root_part, "mountpoint": "/", "fstype": "btrfs"}
                )
            else:
                partitions.append(
                    {
                        "device": _lvm_dev(vg_name, LVM_ROOT),
                        "mountpoint": "/",
                        "fstype": fs,
                    }
                )
                if separate_home:
                    partitions.append(
                        {
                            "device": _lvm_dev(vg_name, LVM_HOME),
                            "mountpoint": "/home",
                            "fstype": fs,
                        }
                    )
        else:
            disk_mib = _disk_size_mib(primary_disk)
            if disk_mib < 10240:
                self.show_toast("Disk is too small (need at least ~10 GiB).")
                return

            if is_uefi:
                efi_part = _part(primary_disk, 1)
                partitions.append(
                    {"device": efi_part, "mountpoint": "/boot/efi", "fstype": "vfat"}
                )
                boot_start = ESP_END_MIB
                boot_part = _part(primary_disk, 2)
                root_part = _part(primary_disk, 3)
                expect_parts = 3
            else:
                boot_start = 3
                boot_part = _part(primary_disk, 2)
                root_part = _part(primary_disk, 3)
                expect_parts = 3

            boot_end = boot_start + BOOT_SIZE_MIB
            commands.extend(
                [
                    ["wipefs", "-af", primary_disk],
                    _sfdisk_cmd(
                        primary_disk,
                        _clean_install_sfdisk_script(
                            is_uefi, root_part_type=root_part_type
                        ),
                    ),
                    ["partprobe", primary_disk],
                    ["udevadm", "settle", "--timeout=30"],
                ]
            )
            if is_uefi:
                commands.append(["mkfs.fat", "-F32", _part(primary_disk, 1)])
            commands.append(["mkfs.ext4", "-F", boot_part])

            usable = max(8192, disk_mib - boot_end - 64)

            partitions.append(
                {"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"}
            )
            if scheme == SCHEME_BTRFS:
                partitions.append(
                    {"device": root_part, "mountpoint": "/", "fstype": "btrfs"}
                )
            else:
                partitions.append(
                    {
                        "device": _lvm_dev(vg_name, LVM_ROOT),
                        "mountpoint": "/",
                        "fstype": fs,
                    }
                )
                if separate_home:
                    partitions.append(
                        {
                            "device": _lvm_dev(vg_name, LVM_HOME),
                            "mountpoint": "/home",
                            "fstype": fs,
                        }
                    )

        config_values = {
            "method": "dual_boot" if dual else "normal",
            "target_disks": [primary_disk],
            "filesystem": fs,
            "storage_scheme": scheme,
            "btrfs_subvolumes": scheme == SCHEME_BTRFS,
            "dual_boot": dual,
            "preserve_efi": preserve,
            "selected_efi_partition": selected_efi if preserve else None,
            "custom_format": False,
            "lvm_thin": scheme == SCHEME_THIN,
            "lvm_vg": vg_name,
            "lvm_pool": LVM_POOL if scheme == SCHEME_THIN else None,
            "lvm_root_lv": LVM_ROOT if vg_name else None,
            "lvm_home_lv": LVM_HOME if separate_home and vg_name else None,
            "lvm_pv": root_part,
            "lvm_usable_mib": usable,
            "separate_boot": True,
            "separate_home": separate_home,
            "expect_partitions": expect_parts,
            "commands": commands,
            "partitions": partitions,
        }
        self.mark_complete_and_return(config_values=config_values)
