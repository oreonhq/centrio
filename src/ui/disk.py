import hashlib
import json
import os
import re
import shlex
import subprocess

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import backend

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


def _mount_source(path):
    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    lines = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not lines:
        return ""
    return lines[0].split("[", 1)[0].strip()


def _whole_disks_of(dev, seen=None):
    seen = set() if seen is None else seen
    if not dev:
        return set()
    name = os.path.basename(os.path.realpath(dev)) if dev.startswith("/dev/") else dev
    sysp = os.path.realpath(f"/sys/class/block/{name}")
    if name in seen or not os.path.isdir(sysp):
        return set()
    seen.add(name)
    if os.path.isfile(os.path.join(sysp, "partition")):
        return {"/dev/" + os.path.basename(os.path.dirname(sysp))}
    found = set()
    try:
        slaves = os.listdir(os.path.join(sysp, "slaves"))
    except OSError:
        slaves = []
    for slave in slaves:
        found |= _whole_disks_of(slave, seen)
    backing = ""
    try:
        with open(os.path.join(sysp, "loop", "backing_file"), encoding="utf-8") as fh:
            backing = fh.read().strip()
    except OSError:
        pass
    if backing:
        found |= _whole_disks_of(_mount_source(backing), seen)
    if not slaves and not backing and not name.startswith(("loop", "dm-", "zram", "ram")):
        found.add("/dev/" + name)
    return found


def _live_medium_disks():
    sources = []
    if _is_live_install_env():
        sources.append(_mount_source("/"))
    for mnt in ("/run/initramfs/live", "/run/initramfs/isoscan", "/run/rootfsbase"):
        if os.path.ismount(mnt):
            sources.append(_mount_source(mnt))
    if os.path.exists("/run/initramfs/livedev"):
        sources.append(os.path.realpath("/run/initramfs/livedev"))
    try:
        with open("/proc/cmdline", encoding="utf-8") as f:
            cmdline = f.read().split()
    except OSError:
        cmdline = []
    links = {
        "CDLABEL": "by-label",
        "LABEL": "by-label",
        "UUID": "by-uuid",
        "PARTUUID": "by-partuuid",
        "PARTLABEL": "by-partlabel",
    }
    for tok in cmdline:
        if not tok.startswith("root=live:"):
            continue
        spec = tok[len("root=live:"):]
        key, _, value = spec.partition("=")
        if spec.startswith("/dev/"):
            sources.append(spec)
        elif value and key.upper() in links:
            sources.append(f"/dev/disk/{links[key.upper()]}/{value}")
    disks = set()
    for src in sources:
        disks |= _whole_disks_of(src)
    return disks


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


def _dual_boot_sfdisk_append(disk, boot_num, root_num, start_mib, boot_end_mib, end_mib, root_part_type=PART_TYPE_LVM):
    root_name = "root" if root_part_type == PART_TYPE_LINUX else "lvm"
    script = (
        f"{_part(disk, boot_num)} : name=boot, start={int(start_mib)}MiB, size={BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
        f"{_part(disk, root_num)} : name={root_name}, start={int(boot_end_mib)}MiB, "
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
            ["vgs", "-o", "name", "--noheadings", "--nolocking",
             "--config", "devices { use_devicesfile = 0 } "
                    "global { event_activation = 0 use_lvmlockd = 0 }"],
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


def _mkfs_cmd(fs, device):
    if fs == "xfs":
        return ["mkfs.xfs", "-f", device]
    if fs == "btrfs":
        return ["mkfs.btrfs", "-f", device]
    return ["mkfs.ext4", "-F", device]


_LVM_CFG_DEVICES = (
    "devices { use_devicesfile = 0 } "
    "global { event_activation = 0 use_lvmlockd = 0 use_lvmpolld = 0 } "
    "activation { auto_activation_volume_list = [] }"
)


def _lvm_flag(args, flag, value):
    if flag not in args:
        args.insert(1, value)
        args.insert(1, flag)
        return
    i = args.index(flag)
    if i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
        args[i + 1] = value
    else:
        args.insert(i + 1, value)


def _lvm_tool(args):
    args = list(args)
    if args[0] != "vgscan" and "-y" not in args and "--yes" not in args:
        args.insert(1, "-y")
    if "--config" not in args:
        args[1:1] = ["--config", _LVM_CFG_DEVICES]
    if args and args[0] == "lvcreate":
        pool_arg = ""
        if "--thinpool" in args:
            i = args.index("--thinpool")
            if i + 1 < len(args):
                pool_arg = str(args[i + 1])
        new_pool = "--thinpool" in args and "/" not in pool_arg
        linear = "--thinpool" not in args
        if new_pool or linear:
            _lvm_flag(args, "--wipesignatures", "n")
            _lvm_flag(args, "--zero", "n")
    return args


def _lv_on(vg_name, lv_name):
    return [
        _lvm_tool(["lvchange", "-ay", f"{vg_name}/{lv_name}"]),
        ["dmsetup", "mknodes"],
    ]


def _legacy_lvm_commands(root_part, vg_name, filesystem, separate_home, usable_mib, scheme):
    fs = (filesystem or "ext4").lower()
    pool_virt = max(8192, int(usable_mib) - 256)
    cmds = [
        ["wipefs", "-af", root_part],
        _lvm_tool(["pvcreate", "-ff", root_part]),
        _lvm_tool(["vgcreate", vg_name, root_part]),
        ["mkdir", "-p", f"/dev/{vg_name}"],
        _lvm_tool(["vgscan", "--mknodes"]),
    ]
    if scheme == SCHEME_THIN:
        cmds.append(
            _lvm_tool(
                [
                    "lvcreate",
                    "-l",
                    "100%FREE",
                    "--poolmetadataspare",
                    "n",
                    "--thinpool",
                    LVM_POOL,
                    vg_name,
                ]
            )
        )
        if separate_home:
            root_v = max(40960, int(pool_virt * 0.4))
            if root_v > pool_virt - 2048:
                root_v = max(8192, pool_virt // 2)
            home_v = max(2048, pool_virt - root_v)
            cmds.append(
                _lvm_tool(
                    [
                        "lvcreate",
                        "-V",
                        f"{root_v}M",
                        "--thin",
                        f"{vg_name}/{LVM_POOL}",
                        "-n",
                        LVM_ROOT,
                    ]
                )
            )
            cmds.append(
                _lvm_tool(
                    [
                        "lvcreate",
                        "-V",
                        f"{home_v}M",
                        "--thin",
                        f"{vg_name}/{LVM_POOL}",
                        "-n",
                        LVM_HOME,
                    ]
                )
            )
            cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_ROOT)))
            cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_HOME)))
        else:
            cmds.append(
                _lvm_tool(
                    [
                        "lvcreate",
                        "-V",
                        f"{pool_virt}M",
                        "--thin",
                        f"{vg_name}/{LVM_POOL}",
                        "-n",
                        LVM_ROOT,
                    ]
                )
            )
            cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_ROOT)))
        return cmds

    if separate_home:
        root_v = max(8192, int(pool_virt * 0.4))
        if root_v > pool_virt - 2048:
            root_v = max(8192, pool_virt // 2)
        cmds.append(
            _lvm_tool(
                [
                    "lvcreate",
                    "-L",
                    f"{root_v}M",
                    "-n",
                    LVM_ROOT,
                    vg_name,
                ]
            )
        )
        cmds.append(
            _lvm_tool(
                [
                    "lvcreate",
                    "-l",
                    "100%FREE",
                    "-n",
                    LVM_HOME,
                    vg_name,
                ]
            )
        )
        cmds.extend(_lv_on(vg_name, LVM_ROOT))
        cmds.extend(_lv_on(vg_name, LVM_HOME))
        cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_ROOT)))
        cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_HOME)))
        return cmds

    cmds.append(
        _lvm_tool(
            [
                "lvcreate",
                "-l",
                "100%FREE",
                "-n",
                LVM_ROOT,
                vg_name,
            ]
        )
    )
    cmds.extend(_lv_on(vg_name, LVM_ROOT))
    cmds.append(_mkfs_cmd(fs, _lvm_dev(vg_name, LVM_ROOT)))
    return cmds


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

    # Partition table commands stay. Root PV/VG/LV commands are rebuilt below.
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
        disk_config["legacy_lvm_commands"] = False
        print(f"Install scheme={scheme} VG={vg_name} PV={root_part} (storage_layout)")
    else:
        disk_config["lvm_vg"] = None
        disk_config["lvm_pool"] = None
        disk_config["lvm_root_lv"] = None
        disk_config["lvm_home_lv"] = None
        disk_config["legacy_lvm_commands"] = False
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


_UNTOUCHABLE_PARTTYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3",
    "af9b60a0-1431-4f62-bc68-3311714a69ad",
}


def get_parent_disk(partition_path):
    if not partition_path:
        return None
    try:
        r = subprocess.run(
            ["lsblk", "-n", "-o", "PKNAME", "-p", partition_path],
            capture_output=True, text=True, timeout=5,
        )
        pk = (r.stdout or "").strip().splitlines()
        if pk and pk[0].strip():
            val = pk[0].strip()
            return val if val.startswith("/dev/") else f"/dev/{val}"
    except Exception:
        pass
    base = os.path.basename(partition_path)
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{base}")))
        if parent and parent != base:
            return f"/dev/{parent}"
    except Exception:
        pass
    m = (
        re.match(r"^(/dev/nvme\d+n\d+)p\d+$", partition_path)
        or re.match(r"^(/dev/mmcblk\d+)p\d+$", partition_path)
        or re.match(r"^(/dev/nbd\d+)p\d+$", partition_path)
        or re.match(r"^(/dev/loop\d+)p\d+$", partition_path)
        or re.match(r"^(/dev/[a-zA-Z]+)\d+$", partition_path)
    )
    return m.group(1) if m else None


def _norm_guid(value):
    return (value or "").lower().replace("-", "").strip()


_EFI_GUID_N = "c12a7328f81f11d2ba4b00a0c93ec93b"


def _udev_part_props(path):
    props = {}
    try:
        r = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", path],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return props
        for line in (r.stdout or "").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    except Exception:
        pass
    return props


def _efi_score(label, typename, size_bytes, fstype):
    score = 10
    blob = f"{label} {typename}".lower()
    if "microsoft" in blob or blob.strip() in ("system", "system_drv", "esp", "efi"):
        score += 50
    if "recovery" in blob or "winre" in blob or "diag" in blob:
        score -= 80
    try:
        n = int(size_bytes or 0)
    except (TypeError, ValueError):
        n = 0
    if 32 * 1024 * 1024 <= n <= 4 * 1024**3:
        score += 15
    if (fstype or "").lower() in ("vfat", "fat32", "fat", "msdos"):
        score += 5
    return score


def _dev_path(dev):
    return (dev.get("path") or dev.get("name") or "").strip()


def _probe_signature(path):
    try:
        proc = backend._sudo_run(["blkid", "-p", "-o", "export", path], run_timeout=10)
    except OSError:
        return None
    if proc is None or proc.returncode not in (0, 2):
        return None
    tags = dict(line.split("=", 1) for line in (proc.stdout or "").splitlines() if "=" in line)
    return (tags.get("TYPE") or tags.get("PTTYPE") or "").lower()


def _partition_reuse_block_reason(path, device, size):
    if not device:
        return "no partition information"
    if size < 9 * 1024**3:
        return "too small"
    if device.get("mountpoint"):
        return "mounted"
    if device.get("children"):
        return "in use"
    if (device.get("parttype") or "").lower() in _UNTOUCHABLE_PARTTYPES:
        return "system partition"
    if "efi" in (device.get("partlabel") or "").lower():
        return "EFI partition"
    fstype = (device.get("fstype") or "").lower()
    if fstype:
        return f"contains {fstype}"
    probed = _probe_signature(path)
    if probed is None:
        return "could not verify that it is empty"
    if probed:
        return f"contains {probed}"
    return None


def get_empty_partition(disk_path):
    if not disk_path or not os.path.exists(disk_path):
        return None
    candidates = []
    for seg in get_disk_layout(disk_path)["segments"]:
        if seg["kind"] != "part":
            continue
        if seg["selectable"]:
            print(f"  empty-part candidate {seg['path']} size={seg['size']}")
            candidates.append((seg["size"], seg["path"]))
        else:
            print(f"  skip empty-part {seg['path']}: {seg['reason']}")
    picked = max(candidates)[1] if candidates else None
    print(f"get_empty_partition({disk_path}) -> {picked}")
    return picked


def _start_to_bytes(start, disk_size):
    try:
        start = int(start)
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return 0
    if start * 512 <= disk_size + 512:
        return start * 512
    return start


def _sysfs_read_int(path):
    with open(path, "r", encoding="utf-8") as f:
        return int((f.read() or "0").strip() or "0")


def _part_belongs_to_disk(part_path, disk_path):
    if not part_path or not disk_path or part_path == disk_path:
        return False
    return os.path.basename(part_path).startswith(os.path.basename(disk_path))


def _best_gap_from_parts(disk_path, disk_size, parts, source):
    if disk_size <= 0:
        print(f"{source} free-space: bad disk size for {disk_path}")
        return None, 0
    if not parts:
        print(f"{source} free-space: no partitions on {disk_path}")
        return None, 0
    ranges = sorted((p[0], p[1]) for p in parts)
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    cursor = 1024 * 1024
    gaps = []
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start, start - cursor))
        cursor = max(cursor, end)
    if disk_size > cursor + 1024 * 1024:
        gaps.append((cursor, disk_size, disk_size - cursor))
    best = None
    best_size = 0
    for gstart, gend, gsize in gaps:
        print(f"  {source} gap on {disk_path}: {gsize // (1024 * 1024)} MiB at {gstart}-{gend}")
        if gsize > best_size:
            best_size = gsize
            best = (gstart, gend, gsize)
    if not best:
        print(f"{source} free-space: no gaps on {disk_path} (disk {disk_size} parts {len(parts)})")
        return None, 0
    start_mib = int((best[0] + 1024 * 1024 - 1) // (1024 * 1024))
    end_mib = int(best[1] // (1024 * 1024))
    size_mib = end_mib - start_mib
    print(f"{source} free space on {disk_path}: {size_mib} MiB at {start_mib}MiB-{end_mib}MiB")
    if size_mib < 9216:
        return None, size_mib
    return (f"{start_mib}MiB", f"{end_mib}MiB"), size_mib


def _free_region_from_sysfs(disk_path):
    base = os.path.basename(disk_path or "")
    sysdir = f"/sys/block/{base}"
    if not base or not os.path.isdir(sysdir):
        print(f"sysfs free-space: {sysdir} missing")
        return None, 0
    try:
        disk_size = _sysfs_read_int(os.path.join(sysdir, "size")) * 512
    except Exception as e:
        print(f"sysfs free-space: disk size failed for {disk_path}: {e}")
        return None, 0
    parts = []
    try:
        names = os.listdir(sysdir)
    except Exception as e:
        print(f"sysfs free-space: listdir failed for {sysdir}: {e}")
        return None, 0
    for name in sorted(names):
        pdir = os.path.join(sysdir, name)
        start_p = os.path.join(pdir, "start")
        size_p = os.path.join(pdir, "size")
        if not os.path.isfile(start_p) or not os.path.isfile(size_p):
            continue
        try:
            start_b = _sysfs_read_int(start_p) * 512
            size_b = _sysfs_read_int(size_p) * 512
        except Exception:
            continue
        if size_b <= 0:
            continue
        print(f"  sysfs part /dev/{name} start={start_b} size={size_b}")
        parts.append((start_b, start_b + size_b, size_b))
    return _best_gap_from_parts(disk_path, disk_size, parts, "sysfs")


def _sysfs_part_start_bytes(dev_path):
    base = os.path.basename(dev_path or "")
    if not base:
        return None
    try:
        return _sysfs_read_int(f"/sys/class/block/{base}/start") * 512
    except Exception:
        return None


def _free_region_from_lsblk(disk_path):
    data = None
    for cols in ("PATH,NAME,TYPE,SIZE,START,PKNAME", "PATH,NAME,TYPE,SIZE,PKNAME"):
        try:
            r = subprocess.run(
                ["lsblk", "-J", "-b", "-p", "-o", cols],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=True,
            )
            data = json.loads(r.stdout or "{}")
            break
        except Exception as e:
            print(f"lsblk free-space probe ({cols}) failed for {disk_path}: {e}")
    if data is None:
        return None, 0
    disk_size = 0
    parts = []
    missing_start = False
    def collect(dev):
        nonlocal disk_size, missing_start
        path = _dev_path(dev)
        typ = (dev.get("type") or "").lower()
        pk = (dev.get("pkname") or "").strip()
        if path == disk_path:
            try:
                disk_size = int(dev.get("size") or 0) or disk_size
            except (TypeError, ValueError):
                pass
        is_part = typ == "part" or _part_belongs_to_disk(path, disk_path)
        if is_part and path != disk_path and (not pk or os.path.basename(disk_path) in pk or _part_belongs_to_disk(path, disk_path)):
            try:
                size = int(dev.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            raw_start = dev.get("start")
            start_b = _sysfs_part_start_bytes(path)
            if start_b is None:
                if raw_start in (None, "", "0"):
                    missing_start = True
                start_b = _start_to_bytes(raw_start, disk_size) if raw_start not in (None, "") else 0
            if size > 0:
                parts.append((start_b, start_b + size, size))
        for child in dev.get("children") or []:
            collect(child)
    for dev in data.get("blockdevices") or []:
        collect(dev)
    if missing_start and parts and all(p[0] == 0 for p in parts) and disk_size:
        used = sum(p[2] for p in parts) + 2 * 1024 * 1024
        free = disk_size - used
        print(f"lsblk has no START offsets on {disk_path}, estimated free {free // (1024**2)} MiB")
        if free < 9216 * 1024 * 1024:
            return None, free // (1024 * 1024)
        start_mib = int((used + 1024 * 1024 - 1) // (1024 * 1024))
        end_mib = int(disk_size // (1024 * 1024))
        return (f"{start_mib}MiB", f"{end_mib}MiB"), end_mib - start_mib
    return _best_gap_from_parts(disk_path, disk_size, parts, "lsblk")


def get_free_space_region(disk_path):
    if not disk_path or not os.path.exists(disk_path):
        return None
    region, _ = _free_region_from_sysfs(disk_path)
    if region:
        return region
    region, _ = _free_region_from_lsblk(disk_path)
    if region:
        return region
    try:
        r = subprocess.run(
            ["parted", "-m", "-s", disk_path, "unit", "MiB", "print", "free"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            print(f"parted free-space probe failed for {disk_path} rc={r.returncode}")
            return None
        best_start, best_end, best_size_mb = None, None, 0
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or line == "BYT;" or line.startswith("/"):
                continue
            fields = line.rstrip(";").split(":")
            if len(fields) < 5:
                continue
            fs = fields[4].strip().lower()
            is_free = fs == "free" or any(f.strip().lower() == "free" for f in fields)
            if not is_free:
                continue
            start_s, end_s, size_s = fields[1], fields[2], fields[3]
            num = _parse_mib(size_s) or 0
            if num > best_size_mb and num >= 9216:
                best_start, best_end, best_size_mb = start_s, end_s, num
        if best_start and best_end:
            return (best_start, best_end)
        return None
    except Exception:
        return None


def get_next_partition_device(disk_path, after=0):
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
        used = set()
        base = disk_path.rsplit("/", 1)[-1]
        for line in (r.stdout or "").splitlines():
            name = line.strip()
            if not name or name == base:
                continue
            if not name.startswith(base):
                continue
            suffix = name[len(base) :].lstrip("p")
            if suffix.isdigit():
                used.add(int(suffix))
        next_num = int(after) + 1
        while next_num in used:
            next_num += 1
        return f"{disk_path}{_partition_prefix(disk_path)}{next_num}"
    except Exception:
        return None


_MIN_FREE_SHOWN_MIB = 16


def get_disk_layout(disk_path):
    layout = {"disk": disk_path, "size": 0, "pttype": "", "segments": []}
    base = os.path.basename(disk_path or "")
    sysdir = f"/sys/block/{base}"
    if not base or not os.path.isdir(sysdir):
        return layout
    try:
        disk_size = _sysfs_read_int(os.path.join(sysdir, "size")) * 512
        names = sorted(os.listdir(sysdir))
    except Exception as e:
        print(f"get_disk_layout({disk_path}) sysfs failed: {e}")
        return layout
    layout["size"] = disk_size
    meta = {}
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-p", "-o", "NAME,PATH,TYPE,FSTYPE,LABEL,PARTTYPE,PARTTYPENAME,PARTLABEL,MOUNTPOINT,PTTYPE", disk_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True,
        )
        for dev in json.loads(r.stdout or "{}").get("blockdevices") or []:
            if _dev_path(dev) == disk_path:
                layout["pttype"] = (dev.get("pttype") or "").lower()
            for child in dev.get("children") or []:
                meta[_dev_path(child)] = child
    except Exception as e:
        print(f"get_disk_layout({disk_path}) lsblk failed: {e}")
    mib = 1024 * 1024
    parts = []
    for name in names:
        pdir = os.path.join(sysdir, name)
        if not os.path.isfile(os.path.join(pdir, "partition")):
            continue
        try:
            number = _sysfs_read_int(os.path.join(pdir, "partition"))
            start = _sysfs_read_int(os.path.join(pdir, "start")) * 512
            size = _sysfs_read_int(os.path.join(pdir, "size")) * 512
        except Exception:
            continue
        if size < mib:
            continue
        path = f"/dev/{name}"
        dev = meta.get(path) or {}
        reason = _partition_reuse_block_reason(path, dev, size)
        parts.append({
            "kind": "part",
            "path": path,
            "number": number,
            "start": start,
            "size": size,
            "fstype": (dev.get("fstype") or "").strip(),
            "label": (dev.get("label") or dev.get("partlabel") or "").strip(),
            "typename": (dev.get("parttypename") or "").strip(),
            "selectable": reason is None,
            "reason": reason,
        })
    parts.sort(key=lambda p: p["start"])
    segments = []
    cursor = mib
    for part in parts + [None]:
        end = part["start"] if part else disk_size
        start_mib = (cursor + mib - 1) // mib
        end_mib = end // mib
        if end_mib - start_mib >= _MIN_FREE_SHOWN_MIB:
            usable = end_mib - start_mib >= 9216
            segments.append({
                "kind": "free",
                "path": None,
                "start": start_mib * mib,
                "size": (end_mib - start_mib) * mib,
                "start_mib": start_mib,
                "end_mib": end_mib,
                "selectable": usable,
                "reason": None if usable else "too small",
            })
        if part:
            segments.append(part)
            cursor = max(cursor, part["start"] + part["size"])
    layout["segments"] = segments
    return layout


def detect_existing_efi_partitions(disk_path=None):
    found = []
    seen = set()

    def add(path, size=None, fstype="vfat", label="", typename=""):
        if not path or path in seen or not path.startswith("/dev/"):
            return
        parent = get_parent_disk(path)
        if disk_path and parent != disk_path and path != disk_path:
            if not _part_belongs_to_disk(path, disk_path):
                return
        seen.add(path)
        rec = {
            "path": path,
            "size": size,
            "fstype": fstype or "vfat",
            "label": label or "",
            "typename": typename or "",
            "parent": parent,
            "score": _efi_score(label, typename, size, fstype),
        }
        print(f"  EFI found {path} parent={parent} label={label!r} type={typename!r} fs={fstype} score={rec['score']}")
        found.append(rec)

    try:
        r = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/boot/efi"],
            capture_output=True, text=True, timeout=5,
        )
        src = (r.stdout or "").strip().split("[", 1)[0].strip()
        if r.returncode == 0 and src.startswith("/dev/"):
            add(src, label="mounted /boot/efi")
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-p", "-o", "NAME,PATH,TYPE,FSTYPE,PARTTYPE,PARTTYPENAME,PARTLABEL,PARTFLAGS,SIZE,PKNAME"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True,
        )
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        print(f"Warning: Failed to detect EFI partitions: {e}")
        data = {}

    def consider(device):
        path = _dev_path(device)
        typ = (device.get("type") or "").lower()
        fstype = (device.get("fstype") or "").lower()
        parttype = (device.get("parttype") or "").lower()
        typename = (device.get("parttypename") or "").lower()
        label = (device.get("partlabel") or "")
        flags = (device.get("partflags") or "").lower()
        size = device.get("size")
        looks_part = typ == "part" or (path and path != get_parent_disk(path) and bool(re.search(r"\d+$", os.path.basename(path or ""))))
        if looks_part and path:
            why = []
            is_efi = False
            if _norm_guid(parttype) == _EFI_GUID_N:
                is_efi = True
                why.append("guid")
            if "efi" in typename:
                is_efi = True
                why.append("typename")
            if "esp" in flags:
                is_efi = True
                why.append("esp-flag")
            lab = label.lower()
            if lab in ("efi", "esp", "system", "system_drv") or "efi system" in lab:
                is_efi = True
                why.append("label")
            if not is_efi:
                props = _udev_part_props(path)
                udev_type = _norm_guid(props.get("ID_PART_ENTRY_TYPE", ""))
                udev_name = (props.get("ID_PART_ENTRY_NAME") or props.get("ID_FS_LABEL") or "").lower()
                udev_fs = (props.get("ID_FS_TYPE") or "").lower()
                if udev_type == _EFI_GUID_N:
                    is_efi = True
                    why.append("udev-guid")
                if "efi" in udev_name or udev_name in ("esp", "system", "system_drv"):
                    is_efi = True
                    why.append("udev-name")
                if not fstype:
                    fstype = udev_fs
            if is_efi:
                print(f"  EFI match {path} via {','.join(why)}")
                add(path, size=size, fstype=fstype or "vfat", label=label, typename=typename)
            else:
                print(f"  not EFI {path} fs={fstype} parttype={parttype} name={typename!r} label={label!r} flags={flags!r}")
        for child in device.get("children") or []:
            consider(child)

    for device in data.get("blockdevices") or []:
        consider(device)

    found.sort(key=lambda e: e.get("score", 0), reverse=True)
    print(f"detect_existing_efi_partitions({disk_path}) -> {[e['path'] for e in found]}")
    return found


def storage_scheme_for_fs(fs):
    fs = (fs or "ext4").lower()
    if fs == "btrfs":
        return SCHEME_BTRFS
    return SCHEME_LVM


def _human_size(num_bytes):
    value = float(num_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _segment_key(seg):
    if seg["kind"] == "free":
        return ("free", seg["start_mib"], seg["end_mib"])
    return ("part", seg["path"])


def _segment_name(seg):
    if seg["kind"] == "free":
        return "Free space"
    return os.path.basename(seg["path"])


def _segment_desc(seg):
    if seg["kind"] == "free":
        return "Unallocated"
    bits = [b for b in (seg["fstype"], seg["label"] or seg["typename"]) if b]
    return ", ".join(bits) or "No filesystem"


def _segment_tooltip(seg):
    lines = [seg["path"] or "Free space", _human_size(seg["size"])]
    if seg["kind"] == "part":
        lines.append(f"Filesystem: {seg['fstype'] or 'none'}")
        if seg["label"]:
            lines.append(f"Label: {seg['label']}")
        if seg["typename"]:
            lines.append(f"Type: {seg['typename']}")
    if not seg["selectable"]:
        lines.append(f"Cannot be used: {seg['reason']}")
    elif seg["kind"] == "part":
        lines.append("Can be used. Its contents will be erased.")
    else:
        lines.append("Can be used.")
    return "\n".join(lines)


class DiskLayoutBar(QWidget):
    selectionChanged = Signal()
    segmentRejected = Signal(object)

    MIN_SEGMENT_PX = 56
    BAR_HEIGHT = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segments = []
        self.selected_index = -1
        self._hover_index = -1
        self._rects = []
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.BAR_HEIGHT)

    def sizeHint(self):
        return QSize(max(1, len(self.segments)) * self.MIN_SEGMENT_PX * 2, self.BAR_HEIGHT)

    def minimumSizeHint(self):
        return QSize(self.MIN_SEGMENT_PX, self.BAR_HEIGHT)

    def set_layout(self, layout, preselect=None):
        self.segments = list(layout.get("segments") or [])
        self.selected_index = -1
        self._hover_index = -1
        self.updateGeometry()
        for i, seg in enumerate(self.segments):
            if seg["selectable"] and preselect is not None and _segment_key(seg) == preselect:
                self.selected_index = i
        self.update()
        self.selectionChanged.emit()

    def selected_segment(self):
        if 0 <= self.selected_index < len(self.segments):
            return self.segments[self.selected_index]
        return None

    def _segment_rects(self):
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        count = len(self.segments)
        if not count or area.width() <= 0:
            return []
        total = sum(max(0, seg["size"]) for seg in self.segments) or 1
        min_w = min(float(self.MIN_SEGMENT_PX), area.width() / count)
        spare = max(0.0, area.width() - min_w * count)
        rects = []
        x = area.left()
        for seg in self.segments:
            w = min_w + spare * max(0, seg["size"]) / total
            rects.append(QRectF(x, area.top(), w, area.height()))
            x += w
        return rects

    def _index_at(self, pos):
        for i, rect in enumerate(self._rects):
            if rect.contains(pos):
                return i
        return -1

    def paintEvent(self, _event):
        painter = QPainter(self)
        pal = self.palette()
        self._rects = self._segment_rects()
        if not self._rects:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No partition information")
            return
        fm = painter.fontMetrics()
        line_h = fm.height()
        for i, (seg, rect) in enumerate(zip(self.segments, self._rects)):
            if i == self.selected_index:
                fill = pal.color(QPalette.ColorRole.Highlight)
                text = pal.color(QPalette.ColorRole.HighlightedText)
            elif seg["kind"] == "free":
                fill = pal.color(QPalette.ColorRole.Base)
                text = pal.color(QPalette.ColorRole.Text)
            elif seg["selectable"]:
                fill = pal.color(QPalette.ColorRole.Button)
                text = pal.color(QPalette.ColorRole.ButtonText)
            else:
                fill = pal.color(QPalette.ColorRole.Mid)
                text = pal.color(QPalette.ColorRole.ButtonText)
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Dark), 1))
            painter.setBrush(fill)
            painter.drawRect(rect)
            if seg["kind"] == "free" and i != self.selected_index:
                painter.fillRect(rect, QBrush(pal.color(QPalette.ColorRole.Mid), Qt.BrushStyle.BDiagPattern))
            if i == self._hover_index and seg["selectable"] and i != self.selected_index:
                painter.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(1, 1, -1, -1))
            inner = rect.adjusted(4, 4, -4, -4)
            if inner.width() < 8:
                continue
            width = int(inner.width())
            lines = [t for t in (_segment_name(seg), _human_size(seg["size"])) if fm.horizontalAdvance(t) <= width]
            desc = fm.elidedText(_segment_desc(seg), Qt.TextElideMode.ElideRight, width)
            if lines and width >= fm.horizontalAdvance("M" * 8):
                lines.append(desc)
            lines = lines[:max(0, int(inner.height() // line_h))]
            top = inner.top() + (inner.height() - len(lines) * line_h) / 2
            painter.setPen(text)
            for n, label in enumerate(lines):
                painter.drawText(
                    QRectF(inner.left(), top + n * line_h, inner.width(), line_h),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )

    def mouseMoveEvent(self, event):
        i = self._index_at(event.position())
        if i != self._hover_index:
            self._hover_index = i
            self.update()
        if i < 0:
            QToolTip.hideText()
            self.unsetCursor()
            return
        seg = self.segments[i]
        if seg["selectable"]:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        QToolTip.showText(event.globalPosition().toPoint(), _segment_tooltip(seg), self)

    def leaveEvent(self, event):
        self._hover_index = -1
        self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        i = self._index_at(event.position())
        if i < 0:
            return
        seg = self.segments[i]
        if not seg["selectable"]:
            self.segmentRejected.emit(seg)
            return
        if i != self.selected_index:
            self.selected_index = i
            self.update()
            self.selectionChanged.emit()



class DiskPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Disk Settings",
            subtitle="Disk selection and partitioning method",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("scrollContent")
        scroll.setWidget(content)
        self.page_layout.addWidget(scroll)
        self.page_layout = QVBoxLayout(content)
        self.page_layout.setContentsMargins(0, 0, 6, 0)
        self.page_layout.setSpacing(10)
        self.disks = self._list_disks()
        self.disk_combo = QComboBox()
        for path, text in self.disks:
            self.disk_combo.addItem(text, path)
        self.refresh_disks_btn = QPushButton("Refresh Disks")
        self.fs_combo = QComboBox()
        self.fs_combo.addItems(["ext4", "xfs", "btrfs"])
        self.dual_boot = QCheckBox("Dual boot mode (use free space, keep other OS)")
        self.preserve_efi = QCheckBox("Preserve existing EFI partition")
        self.preserve_efi.setChecked(True)
        self.separate_home = QCheckBox("Separate /home LV")
        self.layout_label = QLabel("Install location (click free space or an unused partition)")
        self.layout_bar = DiskLayoutBar()
        self._layout_disk = None
        self._efi_status = ""
        self.efi_combo = QComboBox()
        self.efi_label = QLabel("Existing EFI partition")
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("pageSubtitle")

        self.page_layout.addWidget(QLabel("Target disk"))
        disk_row = QHBoxLayout()
        disk_row.addWidget(self.disk_combo, 1)
        disk_row.addWidget(self.refresh_disks_btn)
        self.page_layout.addLayout(disk_row)
        self.page_layout.addWidget(QLabel("Root filesystem"))
        self.page_layout.addWidget(self.fs_combo)
        self.page_layout.addWidget(self.dual_boot)
        self.page_layout.addWidget(self.layout_label)
        self.page_layout.addWidget(self.layout_bar)
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
        self.refresh_disks_btn.clicked.connect(self._reload_disks)
        self.fs_combo.currentTextChanged.connect(self._on_fs_changed)
        self.layout_bar.selectionChanged.connect(self._update_dual_boot_status)
        self.layout_bar.segmentRejected.connect(self._on_layout_segment_rejected)
        self._on_dual_boot_toggled(self.dual_boot.isChecked())
        self._on_fs_changed(self.fs_combo.currentText())

    def refresh_for_network(self):
        return

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_disks()

    def _current_disk(self):
        return (self.disk_combo.currentData() or self.disk_combo.currentText()).strip()

    def _reload_disks(self, *_args):
        current = self._current_disk()
        self.disks = self._list_disks()
        self.disk_combo.blockSignals(True)
        self.disk_combo.clear()
        for path, text in self.disks:
            self.disk_combo.addItem(text, path)
        idx = self.disk_combo.findData(current)
        self.disk_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.disk_combo.blockSignals(False)
        self._refresh_dual_boot_ui()

    def _on_fs_changed(self, *_args):
        fs = (self.fs_combo.currentText() or "ext4").strip().lower()
        scheme = storage_scheme_for_fs(fs)
        if scheme == SCHEME_BTRFS:
            self.separate_home.setChecked(False)
            self.separate_home.setEnabled(False)
            self.separate_home.setText("Separate /home (btrfs uses subvolumes)")
        elif scheme == SCHEME_LVM:
            self.separate_home.setEnabled(True)
            self.separate_home.setText("Separate /home LV")
        else:
            self.separate_home.setEnabled(True)
            self.separate_home.setText("Separate /home LV")
        if not self.dual_boot.isChecked():
            self.status_label.setText("")

    def _list_disks(self):
        live = _live_medium_disks()
        if live:
            print(f"Hiding live boot medium: {sorted(live)}")
        fallback = [] if "/dev/sda" in live else [("/dev/sda", "/dev/sda")]
        try:
            r = subprocess.run(
                ["lsblk", "-J", "-b", "-d", "-o", "PATH,NAME,TYPE,SIZE,RO,MODEL,TRAN"],
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
                name = str(dev.get("name", "")).strip()
                if not name or name.startswith(skip_prefixes):
                    continue
                size = int(dev.get("size", 0) or 0)
                if size < min_size_bytes:
                    continue
                path = str(dev.get("path", "")).strip()
                if not path.startswith("/dev/") or os.path.realpath(path) in live:
                    continue
                model = " ".join(str(dev.get("model") or "").split())
                desc = ", ".join(b for b in (model, _human_size(size)) if b)
                text = f"{path} ({desc})"
                if str(dev.get("tran") or "").lower() == "usb":
                    text += " (USB)"
                elif backend.disk_is_portable(path):
                    text += " (removable)"
                disks.append((path, text))
            return disks or fallback
        except Exception:
            return fallback

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
        self.layout_label.setVisible(dual)
        self.layout_bar.setVisible(dual)

        if not dual:
            self.status_label.setText("")
            return

        disk = self._current_disk()
        previous = self.layout_bar.selected_segment()
        preselect = _segment_key(previous) if previous and self._layout_disk == disk else None
        layout = get_disk_layout(disk) if disk else {"segments": []}
        if preselect is None and disk:
            empty = get_empty_partition(disk)
            free = [s for s in layout["segments"] if s["kind"] == "free" and s["selectable"]]
            if empty:
                preselect = ("part", empty)
            elif free:
                preselect = _segment_key(max(free, key=lambda s: s["size"]))
        self._layout_disk = disk

        efi_list = detect_existing_efi_partitions(
            disk if disk and backend.disk_is_portable(disk) else None
        )
        self.efi_combo.clear()
        for e in efi_list:
            extra = " ".join(x for x in (e.get("label"), e.get("parent")) if x)
            text = e["path"] if not extra else f"{e['path']}  ({extra})"
            self.efi_combo.addItem(text, e["path"])
        if self.preserve_efi.isChecked() and not efi_list:
            self._efi_status = "No EFI partition found."
        elif efi_list:
            self._efi_status = f"Found {len(efi_list)} EFI partition(s)."
        else:
            self._efi_status = ""
        self.layout_bar.set_layout(layout, preselect)

    def _update_dual_boot_status(self):
        if not self.dual_boot.isChecked():
            return
        seg = self.layout_bar.selected_segment()
        if seg is None:
            if any(s["selectable"] for s in self.layout_bar.segments):
                msgs = ["Select free space or an unused partition in the disk layout."]
            else:
                msgs = ["No usable free space or unused partition (>= 9 GiB). Shrink a partition first."]
        elif seg["kind"] == "free":
            msgs = [f"Install into free space: {_human_size(seg['size'])} at {seg['start_mib']} MiB."]
        else:
            msgs = [f"Install into {seg['path']} ({_human_size(seg['size'])}). Its contents will be erased."]
        if self._efi_status:
            msgs.append(self._efi_status)
        self.status_label.setText(" ".join(msgs))

    def _on_layout_segment_rejected(self, seg):
        self.show_toast(f"{_segment_name(seg)} cannot be used: {seg['reason']}")

    def apply_settings_and_return(self, _button=None):
        primary_disk = self._current_disk()
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
            layout = get_disk_layout(primary_disk)
            chosen = self.layout_bar.selected_segment()
            if chosen and self._layout_disk == primary_disk:
                current = {_segment_key(s): s for s in layout["segments"]}
                chosen = current.get(_segment_key(chosen))
            else:
                chosen = None
            if not chosen or not chosen["selectable"]:
                self._refresh_dual_boot_ui()
                self.show_toast(
                    "Dual boot needs free space or an unused partition (>= 9 GiB). Select one in the disk layout."
                )
                return
            empty = chosen["path"] if chosen["kind"] == "part" else None
            region = None if empty else (f"{chosen['start_mib']}MiB", f"{chosen['end_mib']}MiB")
            portable = backend.disk_is_portable(primary_disk)
            efi_list = detect_existing_efi_partitions(primary_disk if portable else None)
            selected_efi = self.efi_combo.currentData()
            if not selected_efi:
                raw = self.efi_combo.currentText().strip()
                selected_efi = raw.split()[0] if raw else None
            if portable and selected_efi not in {e["path"] for e in efi_list}:
                selected_efi = None
            if not selected_efi and efi_list:
                selected_efi = efi_list[0]["path"]
            if not selected_efi:
                self.show_toast("No existing EFI partition found to preserve.")
                return
            partitions.append(
                {"device": selected_efi, "mountpoint": "/boot/efi", "fstype": "vfat"}
            )
            if empty:
                root_part = empty
                boot_part = None
                expect_parts = int(re.search(r"(\d+)$", empty).group(1))
                try:
                    sz = subprocess.run(
                        ["lsblk", "-b", "-dn", "-o", "SIZE", empty],
                        capture_output=True, text=True, timeout=5, check=True,
                    )
                    usable = max(8192, int((sz.stdout or "0").strip() or "0") // (1024 * 1024) - 64)
                except Exception:
                    usable = 8192
                if scheme == SCHEME_BTRFS:
                    commands.append(["mkfs.btrfs", "-f", empty])
                    partitions.append(
                        {"device": empty, "mountpoint": "/", "fstype": "btrfs"}
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
                if layout["pttype"] != "gpt":
                    self.show_toast("Installing into free space requires a GPT partition table.")
                    return
                boot_part = get_next_partition_device(primary_disk)
                if not boot_part:
                    self.show_toast("Could not determine the next partition device.")
                    return
                if os.path.exists(boot_part):
                    self.show_toast("Refusing to format an existing partition for dual boot.")
                    return
                boot_num = int(re.search(r"(\d+)$", boot_part).group(1))
                root_part = get_next_partition_device(primary_disk, boot_num)
                if not root_part:
                    self.show_toast("Could not determine the next partition device.")
                    return
                root_num = int(re.search(r"(\d+)$", root_part).group(1))
                if os.path.exists(root_part):
                    self.show_toast("Refusing to format an existing partition for dual boot.")
                    return

                start_mib = _parse_mib(region[0])
                end_mib = _parse_mib(region[1])
                if start_mib is None or end_mib is None or (end_mib - start_mib) < 9216:
                    self.show_toast("Free space is too small for /boot + root.")
                    return
                boot_end_mib = start_mib + BOOT_SIZE_MIB
                expect_parts = root_num
                commands.extend(
                    [
                        _dual_boot_sfdisk_append(
                            primary_disk,
                            boot_num,
                            root_num,
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
            "separate_boot": bool(boot_part) if dual else True,
            "separate_home": separate_home,
            "expect_partitions": expect_parts,
            "commands": commands,
            "partitions": partitions,
        }
        self.mark_complete_and_return(config_values=config_values)
