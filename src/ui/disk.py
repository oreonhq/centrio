# Centrio Installer
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# centrio_installer/ui/disk.py

import gi
import subprocess # For running lsblk
import json       # For parsing lsblk output
import shlex      # For safe command string generation
import os         # For path manipulation
import re # For parsing losetup
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .base import BaseConfigurationPage
import backend
import disk_filter
# D-Bus imports are no longer needed here
# from ..utils import dasbus, DBusError, dbus_available 
# from ..constants import (...) 

# Helper function to format size
def format_bytes(size_bytes):
    if size_bytes is None:
        return "N/A"
    # Simple GB conversion for display
    gb = size_bytes / (1024**3)
    if gb < 0.1:
        mb = size_bytes / (1024**2)
        return f"{mb:.1f} MiB"
    return f"{gb:.1f} GiB"

ESP_END_MIB = 513
BOOT_SIZE_MIB = 2048
MIN_FREE_MIB = BOOT_SIZE_MIB + 8192

PART_TYPE_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
PART_TYPE_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
PART_TYPE_BIOS_BOOT = "21686148-6449-6E6F-744E-656564454649"


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


def _clean_install_sfdisk_script(is_uefi, efi_size_mib=512):
    if is_uefi:
        boot_start = 1 + efi_size_mib
        return (
            "label: gpt\n"
            f"name=ESP, start=1MiB, size={efi_size_mib}MiB, type={PART_TYPE_ESP}\n"
            f"name=boot, start={boot_start}MiB, size={BOOT_SIZE_MIB}MiB, "
            f"type={PART_TYPE_LINUX}\n"
            f"name=root, start={boot_start + BOOT_SIZE_MIB}MiB, "
            f"type={PART_TYPE_LINUX}\n"
        )
    return (
        "label: gpt\n"
        f"name=biosboot, start=1MiB, size=2MiB, type={PART_TYPE_BIOS_BOOT}\n"
        f"name=boot, start=3MiB, size={BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
        f"name=root, start={3 + BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
    )


def _dual_boot_sfdisk_append(disk, boot_num, root_num, start_mib, boot_end_mib, end_mib):
    script = (
        f"{_part(disk, boot_num)} : name=boot, start={int(start_mib)}MiB, size={BOOT_SIZE_MIB}MiB, "
        f"type={PART_TYPE_LINUX}\n"
        f"{_part(disk, root_num)} : name=root, start={int(boot_end_mib)}MiB, "
        f"size={int(end_mib - boot_end_mib)}MiB, "
        f"type={PART_TYPE_LINUX}\n"
    )
    return [
        "bash",
        "-c",
        f"sfdisk --append --force {shlex.quote(disk)} <<'EOF'\n{script}EOF",
    ]


def _mkfs_root_command(filesystem, device):
    if filesystem == "btrfs":
        return ["mkfs.btrfs", "-f", device]
    if filesystem == "xfs":
        return ["mkfs.xfs", "-f", device]
    return ["mkfs.ext4", "-F", device]


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


_UNTOUCHABLE_PARTTYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3",
    "af9b60a0-1431-4f62-bc68-3311714a69ad",
}


def _dev_path(dev):
    return (dev.get("path") or dev.get("name") or "").strip()


def _sysfs_read_int(path):
    with open(path, "r", encoding="utf-8") as f:
        return int((f.read() or "0").strip() or "0")


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
            usable = end_mib - start_mib >= MIN_FREE_MIB
            reason = None if usable else "too small"
            if usable and layout["pttype"] != "gpt":
                usable = False
                reason = "requires a GPT partition table"
            segments.append({
                "kind": "free",
                "path": None,
                "start": start_mib * mib,
                "size": (end_mib - start_mib) * mib,
                "start_mib": start_mib,
                "end_mib": end_mib,
                "selectable": usable,
                "reason": reason,
            })
        if part:
            segments.append(part)
            cursor = max(cursor, part["start"] + part["size"])
    layout["segments"] = segments
    return layout


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

# --- Helper Functions to Check Host Usage ---

def get_host_mounts():
    """Gets currently mounted filesystems on the host."""
    mounts = {}
    try:
        cmd = ["findmnt", "-J", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
        mount_data = json.loads(result.stdout)
        if "filesystems" in mount_data:
            for fs in mount_data["filesystems"]:
                source = fs.get("source")
                target = fs.get("target")
                if source and target:
                    mounts[target] = source 
        print(f"Detected host mounts: {mounts}")
        return mounts
    except Exception as e:
        print(f"Warning: Failed to get host mounts using findmnt: {e}")
        return {}

def get_host_lvm_pvs():
    """Gets active LVM Physical Volumes on the host."""
    pvs = set()
    try:
        cmd = ["pvs", "--noheadings", "-o", "pv_name"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
        for line in result.stdout.splitlines():
            pv_name = line.strip()
            if pv_name:
                try:
                    real_path = os.path.realpath(pv_name)
                    pvs.add(real_path)
                except Exception:
                    pvs.add(pv_name)
        print(f"Detected host LVM PVs: {pvs}")
        return pvs
    except Exception as e:
        print(f"Warning: Failed to get host LVM PVs: {e}")
        return set()

def disk_has_unallocated_space(disk_path):
    """Check if a disk has usable free space or an unused partition for dual boot."""
    return any(s["selectable"] for s in get_disk_layout(disk_path)["segments"])


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


def get_parent_disk(partition_path):
    """Get the parent disk path for a partition (e.g. /dev/sda1 -> /dev/sda)."""
    if not partition_path:
        return None
    m = re.match(r"^(/dev/[a-zA-Z]+)\d*$", partition_path) or \
        re.match(r"^(/dev/nvme\d+n\d+)p?\d*$", partition_path) or \
        re.match(r"^(/dev/mmcblk\d+)p?\d*$", partition_path)
    return m.group(1) if m else None


def detect_existing_efi_partitions(disk_path=None):
    """Detect existing EFI system partitions that could be reused for dual boot."""
    efi_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    efi_partitions = []
    seen_paths = set()
    try:
        # Fallback: if /boot/efi is mounted, use that partition
        mounted = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "/boot/efi"],
            capture_output=True, text=True, timeout=5
        )
        if mounted.returncode == 0 and (mounted.stdout or "").strip():
            src = mounted.stdout.strip()
            if disk_path and get_parent_disk(src) != disk_path:
                src = ""
            if src and src not in seen_paths:
                seen_paths.add(src)
                efi_partitions.append({"path": src, "size": None, "fstype": "vfat"})

        result = subprocess.run(
            ["lsblk", "-J", "-o", "PATH,FSTYPE,PARTTYPE,SIZE,PARTFLAGS"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True
        )
        lsblk_data = json.loads(result.stdout or "{}")

        def scan_device(device):
            path = device.get("path")
            fstype = (device.get("fstype") or "").lower()
            parttype = (device.get("parttype") or "").lower()
            flags = (device.get("partflags") or "").lower()
            size = device.get("size")
            if not path or path in seen_paths:
                return
            is_efi = (
                fstype == "vfat" and parttype == efi_guid
            ) or (
                fstype == "vfat" and ("esp" in flags or "boot" in flags)
            )
            if is_efi:
                if disk_path and get_parent_disk(path) != disk_path:
                    return
                seen_paths.add(path)
                efi_partitions.append({"path": path, "size": size, "fstype": fstype or "vfat"})
            for child in device.get("children", []):
                scan_device(child)

        for device in lsblk_data.get("blockdevices", []):
            scan_device(device)
    except Exception as e:
        print(f"Warning: Failed to detect EFI partitions: {e}")
    return efi_partitions

class DiskPage(BaseConfigurationPage):
    LAYOUT_MIN_SEGMENT_PX = 56
    LAYOUT_BAR_HEIGHT = 72

    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(title="Installation Destination", subtitle="Configure disk partitioning and filesystem", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        
        # State variables
        self.detected_disks = []
        self.selected_disks = set()
        self.scan_completed = False
        self.partitioning_method = None
        self.filesystem_type = "ext4"
        self.dual_boot_enabled = False
        self.preserve_efi = False
        self.selected_efi_partition = None
        self.custom_format_enabled = False
        self.disk_widgets = {}
        self.disk_list_rows = []  # Track rows for proper cleanup on rescan
        self.efi_partitions = []
        self.disks_with_free_space = set()
        self.layout_segments = []
        self.layout_selected = -1
        self._layout_disk = None
        
        self._build_ui()
            
    def _build_ui(self):
        """Build the enhanced disk configuration UI."""
        
        # Initial scan section
        info_group = Adw.PreferencesGroup()
        self.add(info_group)
        
        scan_row = Adw.ActionRow(
            title="Storage Device Detection",
            subtitle="Scan for available storage devices"
        )
        self.scan_button = Gtk.Button(label="Scan for Disks")
        self.scan_button.set_valign(Gtk.Align.CENTER)
        self.scan_button.add_css_class("suggested-action")
        self.scan_button.add_css_class("compact")
        self.scan_button.connect("clicked", self.scan_for_disks)
        scan_row.add_suffix(self.scan_button)
        self.refresh_disks_button = Gtk.Button(label="Refresh Disks")
        self.refresh_disks_button.set_valign(Gtk.Align.CENTER)
        self.refresh_disks_button.add_css_class("compact")
        self.refresh_disks_button.connect("clicked", lambda _b: self.scan_for_disks(self.scan_button))
        scan_row.add_suffix(self.refresh_disks_button)
        info_group.add(scan_row)

        # Disk selection section
        self.disk_list_group = Adw.PreferencesGroup(title="Available Disks")
        self.disk_list_group.set_description("Select disk(s) for installation")
        self.disk_list_group.set_visible(False)
        self.add(self.disk_list_group)
        
        # Installation mode section
        self.mode_group = Adw.PreferencesGroup(title="Installation Mode")
        self.mode_group.set_visible(False)
        self.add(self.mode_group)
        
        # Normal installation
        self.normal_install_row = Adw.ActionRow(
            title="Clean Installation",
            subtitle="Erase disk and install Centrio (recommended)"
        )
        self.normal_radio = Gtk.CheckButton()
        self.normal_radio.set_valign(Gtk.Align.CENTER)
        self.normal_radio.connect("toggled", self.on_install_mode_changed, "normal")
        self.normal_install_row.add_suffix(self.normal_radio)
        self.normal_install_row.set_activatable_widget(self.normal_radio)
        self.mode_group.add(self.normal_install_row)
        
        # Dual boot installation
        self.dual_boot_row = Adw.ActionRow(
            title="Dual Boot Installation",
            subtitle="Install alongside Windows or another OS, reusing the existing EFI partition. Requires unallocated disk space (shrink a partition in Windows Disk Management or GParted first)."
        )
        self.dual_boot_radio = Gtk.CheckButton(group=self.normal_radio)
        self.dual_boot_radio.set_valign(Gtk.Align.CENTER)
        self.dual_boot_radio.connect("toggled", self.on_install_mode_changed, "dual_boot")
        self.dual_boot_row.add_suffix(self.dual_boot_radio)
        self.dual_boot_row.set_activatable_widget(self.dual_boot_radio)
        self.mode_group.add(self.dual_boot_row)

        self.layout_group = Adw.PreferencesGroup(
            title="Install Location",
            description="Click free space or an unused partition"
        )
        self.layout_group.set_visible(False)
        self.add(self.layout_group)
        self.layout_area = Gtk.DrawingArea()
        self.layout_area.set_content_height(self.LAYOUT_BAR_HEIGHT)
        self.layout_area.set_hexpand(True)
        self.layout_area.set_draw_func(self._draw_layout)
        self.layout_area.set_has_tooltip(True)
        self.layout_area.connect("query-tooltip", self._on_layout_query_tooltip)
        layout_click = Gtk.GestureClick()
        layout_click.connect("pressed", self._on_layout_pressed)
        self.layout_area.add_controller(layout_click)
        layout_scroller = Gtk.ScrolledWindow()
        layout_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        layout_scroller.set_min_content_height(self.LAYOUT_BAR_HEIGHT + 16)
        layout_scroller.set_child(self.layout_area)
        self.layout_group.add(layout_scroller)
        self.layout_status = Gtk.Label(xalign=0)
        self.layout_status.set_wrap(True)
        self.layout_status.set_margin_top(6)
        self.layout_group.add(self.layout_status)
        
        # EFI partition selection (for dual boot) - placed right after mode so it's visible when dual boot selected
        self.efi_group = Adw.PreferencesGroup(
            title="EFI Partition Selection",
            description="Select an existing EFI system partition to reuse (required for dual boot)"
        )
        self.efi_group.set_visible(False)
        self.add(self.efi_group)
        
        # Filesystem selection section
        self.fs_group = Adw.PreferencesGroup(title="Filesystem Configuration")
        self.fs_group.set_visible(False)
        self.add(self.fs_group)
        
        # Filesystem type selection
        fs_row = Adw.ComboRow(title="Root Filesystem Type")
        fs_row.set_subtitle("Choose the filesystem for the root partition")
        fs_model = Gtk.StringList()
        fs_model.append("ext4 (default)")
        fs_model.append("btrfs")
        fs_model.append("xfs")
        fs_row.set_model(fs_model)
        fs_row.set_selected(0)  # Default to ext4
        fs_row.connect("notify::selected", self.on_filesystem_changed)
        self.fs_row = fs_row
        self.fs_group.add(fs_row)

        # BTRFS snapshots (subvolumes + snapper) - only visible when btrfs selected
        self.btrfs_snapshots_row = Adw.SwitchRow(
            title="BTRFS Snapshots (Rollback)",
            subtitle="BTRFS root subvolume + snapper rollback; /home on root (reliable boot)"
        )
        self.btrfs_snapshots_row.set_active(True)
        self.btrfs_snapshots_row.connect("notify::active", self._on_btrfs_snapshots_toggled)
        self.fs_group.add(self.btrfs_snapshots_row)
        self.btrfs_snapshots_enabled = True
        self._btrfs_snapshots_subtitle = "BTRFS root subvolume + snapper rollback; /home on root (reliable boot)"
        
        # Custom formatting toggle
        self.custom_format_row = Adw.SwitchRow(
            title="Custom Formatting Options",
            subtitle="Enable advanced formatting and partition options"
        )
        self.custom_format_row.connect("notify::active", self.on_custom_format_toggled)
        self.fs_group.add(self.custom_format_row)
        
        # Advanced options (shown when custom formatting is enabled)
        self.advanced_group = Adw.PreferencesGroup(title="Advanced Options")
        self.advanced_group.set_visible(False)
        self.add(self.advanced_group)
        
        # EFI partition size (for custom formatting)
        self.efi_size_row = Adw.SpinRow(
            title="EFI Partition Size",
            subtitle="Size in MB for the EFI system partition"
        )
        adjustment = Gtk.Adjustment(value=512, lower=100, upper=2048, step_increment=50)
        self.efi_size_row.set_adjustment(adjustment)
        self.advanced_group.add(self.efi_size_row)
        
        # Confirm button
        self.button_group = Adw.PreferencesGroup()
        self.add(self.button_group)
        
        confirm_row = Adw.ActionRow(
            title="Confirm Storage Configuration",
            subtitle="Review and apply your storage settings"
        )
        self.complete_button = Gtk.Button(label="Apply Storage Plan")
        self.complete_button.set_valign(Gtk.Align.CENTER)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.add_css_class("compact")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        self.complete_button.set_sensitive(False)
        confirm_row.add_suffix(self.complete_button)
        self.button_group.add(confirm_row)

        # No _connect_dbus needed anymore
        # self._connect_dbus() 
            
    def _get_disks_with_free_space(self):
        """Return set of disk paths that have unallocated space (for dual boot)."""
        result = set()
        for disk in self.detected_disks:
            path = disk.get("path")
            if path and not disk.get("is_live_os_disk") and disk_has_unallocated_space(path):
                result.add(path)
        return result

    def _check_dual_boot_available(self):
        """Check if dual boot is possible: EFI partitions exist and at least one disk has unallocated space."""
        if not os.path.exists("/sys/firmware/efi"):
            self.dual_boot_row.set_sensitive(False)
            self.dual_boot_row.set_subtitle("Dual boot requires UEFI firmware.")
            return False
        selected = next(iter(self.selected_disks), None)
        self.efi_partitions = detect_existing_efi_partitions(selected)
        self.disks_with_free_space = self._get_disks_with_free_space()
        if not self.efi_partitions:
            self.dual_boot_row.set_sensitive(False)
            self.dual_boot_row.set_subtitle("No existing EFI partitions found. Use clean installation.")
            return False
        if not self.disks_with_free_space:
            self.dual_boot_row.set_sensitive(False)
            self.dual_boot_row.set_subtitle("No disk has unallocated space. Shrink a partition in Windows Disk Management or GParted first.")
            return False
        self.dual_boot_row.set_sensitive(True)
        self.dual_boot_row.set_subtitle("Select a disk with free space, then choose EFI partition below.")
        return True

    def on_install_mode_changed(self, button, mode):
        """Handle installation mode selection."""
        if not button.get_active():
            return

        if mode == "dual_boot" and not self.dual_boot_row.get_sensitive():
            self.normal_radio.set_active(True)
            return

        if mode == "normal":
            self.dual_boot_enabled = False
            self.preserve_efi = False
            self.efi_group.set_visible(False)
            self.layout_group.set_visible(False)
            self._update_btrfs_snapshots_for_mode()
            for disk_path, widget_info in self.disk_widgets.items():
                disk = next((d for d in self.detected_disks if d["path"] == disk_path), None)
                if disk and not disk.get("is_live_os_disk"):
                    widget_info["row"].set_sensitive(True)
                    widget_info["row"].set_subtitle(format_bytes(disk.get("size")))
            print("Installation mode: Normal (clean installation)")
        elif mode == "dual_boot":
            self.dual_boot_enabled = True
            if self._check_dual_boot_available():
                self._update_disk_list_for_dual_boot()
                self._populate_efi_partitions()
                self.efi_group.set_visible(True)
                self._refresh_layout()
                self.layout_group.set_visible(True)
                if self.efi_partitions and not self.selected_efi_partition:
                    self.selected_efi_partition = self.efi_partitions[0].get("path")
                    self.preserve_efi = True
                print(f"Installation mode: Dual boot (found {len(self.efi_partitions)} EFI partitions)")
                self.btrfs_snapshots_row.set_sensitive(False)
                self.btrfs_snapshots_row.set_subtitle("Requires clean installation (separate /boot)")
                self.btrfs_snapshots_row.set_active(False)
                self.btrfs_snapshots_enabled = False
            else:
                self.normal_radio.set_active(True)
                return

        self.partitioning_method = mode
        self.update_complete_button_state()
    
    def on_filesystem_changed(self, combo_row, pspec):
        """Handle filesystem type selection."""
        selected = combo_row.get_selected()
        fs_types = ["ext4", "btrfs", "xfs"]
        self.filesystem_type = fs_types[selected] if selected < len(fs_types) else "ext4"
        self.btrfs_snapshots_row.set_visible(self.filesystem_type == "btrfs")
        print(f"Selected filesystem: {self.filesystem_type}")
        self.update_complete_button_state()

    def _on_btrfs_snapshots_toggled(self, switch_row, pspec):
        """Handle BTRFS snapshots toggle."""
        self.btrfs_snapshots_enabled = switch_row.get_active()
        print(f"BTRFS snapshots (rollback): {'enabled' if self.btrfs_snapshots_enabled else 'disabled'}")

    def _update_btrfs_snapshots_for_mode(self):
        """Restore BTRFS snapshots row when switching to normal (dual boot disables it)."""
        if self.filesystem_type == "btrfs" and not self.dual_boot_enabled:
            self.refresh_for_network()
        elif self.filesystem_type != "btrfs":
            self.btrfs_snapshots_row.set_visible(False)

    def refresh_for_network(self):
        """Gray out BTRFS snapshots option when no network (requires DNF for snapper packages)."""
        has_network = backend.check_network_connectivity()
        net = self.main_window.final_config.get("network", {}) if self.main_window else {}
        if net.get("skip_network", False):
            has_network = False
        elif net.get("network_status") == "connected":
            has_network = True
        self.btrfs_snapshots_row.set_sensitive(has_network)
        if has_network:
            self.btrfs_snapshots_row.set_subtitle(self._btrfs_snapshots_subtitle)
        else:
            self.btrfs_snapshots_row.set_subtitle("Requires network connection (snapper packages)")
            self.btrfs_snapshots_row.set_active(False)
            self.btrfs_snapshots_enabled = False
    
    def on_custom_format_toggled(self, switch_row, pspec):
        """Handle custom formatting toggle."""
        self.custom_format_enabled = switch_row.get_active()
        self.advanced_group.set_visible(self.custom_format_enabled)
        print(f"Custom formatting: {self.custom_format_enabled}")
    
    def _update_disk_list_for_dual_boot(self):
        """When dual boot is selected, only enable disks with free space; clear invalid selection."""
        for disk_path, widget_info in self.disk_widgets.items():
            row, radio = widget_info["row"], widget_info["radio"]
            disk = next((d for d in self.detected_disks if d["path"] == disk_path), None)
            size_str = format_bytes(disk["size"]) if disk else "N/A"
            if disk_path in self.disks_with_free_space:
                row.set_sensitive(True)
                row.set_subtitle(f"{size_str}, has free space or an unused partition")
            else:
                row.set_sensitive(False)
                row.set_subtitle(f"{size_str}, no usable space (shrink a partition first)")
                if radio.get_active():
                    radio.set_active(False)
                    self.selected_disks.discard(disk_path)
        if not self.selected_disks and self.disks_with_free_space:
            first_valid = next(iter(self.disks_with_free_space))
            w = self.disk_widgets.get(first_valid)
            if w:
                w["radio"].set_active(True)
                self.selected_disks.add(first_valid)
            self.show_toast("Select a disk with free space for dual boot")

    def _selected_segment(self):
        if 0 <= self.layout_selected < len(self.layout_segments):
            return self.layout_segments[self.layout_selected]
        return None

    def _refresh_layout(self):
        disk = next(iter(self.selected_disks), None)
        previous = self._selected_segment()
        preselect = _segment_key(previous) if previous and self._layout_disk == disk else None
        segments = get_disk_layout(disk)["segments"] if disk else []
        if preselect is None:
            parts = [s for s in segments if s["kind"] == "part" and s["selectable"]]
            free = [s for s in segments if s["kind"] == "free" and s["selectable"]]
            if parts:
                preselect = _segment_key(max(parts, key=lambda s: s["size"]))
            elif free:
                preselect = _segment_key(max(free, key=lambda s: s["size"]))
        self.layout_segments = segments
        self.layout_selected = next(
            (i for i, s in enumerate(segments) if s["selectable"] and _segment_key(s) == preselect), -1
        )
        self._layout_disk = disk
        self.layout_area.set_content_width(max(1, len(segments)) * self.LAYOUT_MIN_SEGMENT_PX * 2)
        self.layout_area.queue_draw()
        self._update_layout_status()

    def _update_layout_status(self):
        seg = self._selected_segment()
        if seg is None:
            if any(s["selectable"] for s in self.layout_segments):
                msg = "Select free space or an unused partition in the disk layout."
            else:
                msg = f"No usable free space (at least {MIN_FREE_MIB // 1024} GiB) or unused partition (at least 9 GiB). Shrink a partition first."
        elif seg["kind"] == "free":
            msg = f"Install into free space: {_human_size(seg['size'])} at {seg['start_mib']} MiB."
        else:
            msg = f"Install into {seg['path']} ({_human_size(seg['size'])}). Its contents will be erased."
        self.layout_status.set_text(msg)

    def _layout_rects(self, width, height):
        count = len(self.layout_segments)
        width -= 2
        if not count or width <= 0:
            return []
        total = sum(max(0, seg["size"]) for seg in self.layout_segments) or 1
        min_w = min(float(self.LAYOUT_MIN_SEGMENT_PX), width / count)
        spare = max(0.0, width - min_w * count)
        rects = []
        x = 1.0
        for seg in self.layout_segments:
            w = min_w + spare * max(0, seg["size"]) / total
            rects.append((x, 1.0, w, height - 2.0))
            x += w
        return rects

    def _layout_index_at(self, x, y):
        rects = self._layout_rects(self.layout_area.get_width(), self.layout_area.get_height())
        for i, (rx, ry, rw, rh) in enumerate(rects):
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return i
        return -1

    def _draw_layout(self, area, cr, width, height):
        rects = self._layout_rects(width, height)
        cr.set_font_size(11)
        if not rects:
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.move_to(8, height / 2)
            cr.show_text("No partition information")
            return
        for i, (seg, (x, y, w, h)) in enumerate(zip(self.layout_segments, rects)):
            if i == self.layout_selected:
                fill, text = (0.21, 0.52, 0.89), (1, 1, 1)
            elif seg["kind"] == "free":
                fill, text = (0.92, 0.92, 0.92), (0.1, 0.1, 0.1)
            elif seg["selectable"]:
                fill, text = (0.75, 0.75, 0.75), (0.1, 0.1, 0.1)
            else:
                fill, text = (0.45, 0.45, 0.45), (1, 1, 1)
            cr.rectangle(x, y, w, h)
            cr.set_source_rgb(*fill)
            cr.fill_preserve()
            cr.set_source_rgb(0.3, 0.3, 0.3)
            cr.set_line_width(1)
            cr.stroke()
            if w < 16:
                continue
            cr.save()
            cr.rectangle(x + 4, y, w - 8, h)
            cr.clip()
            cr.set_source_rgb(*text)
            ty = y + h / 2 - 4
            for line in (_segment_name(seg), _human_size(seg["size"])):
                cr.move_to(x + 6, ty)
                cr.show_text(line)
                ty += 16
            cr.restore()

    def _on_layout_pressed(self, gesture, n_press, x, y):
        idx = self._layout_index_at(x, y)
        if idx < 0:
            return
        seg = self.layout_segments[idx]
        if not seg["selectable"]:
            self.show_toast(f"{_segment_name(seg)} cannot be used: {seg['reason']}")
            return
        self.layout_selected = idx
        self.layout_area.queue_draw()
        self._update_layout_status()
        self.update_complete_button_state()

    def _on_layout_query_tooltip(self, area, x, y, keyboard_mode, tooltip):
        idx = self._layout_index_at(x, y)
        if idx < 0:
            return False
        tooltip.set_text(_segment_tooltip(self.layout_segments[idx]))
        return True

    def on_efi_partition_selected(self, button, partition_path):
        """Handle EFI partition selection for dual boot."""
        if button.get_active():
            self.selected_efi_partition = partition_path
            self.preserve_efi = True
            print(f"Selected EFI partition: {partition_path}")
        else:
            if self.selected_efi_partition == partition_path:
                self.selected_efi_partition = None
                self.preserve_efi = False
        self.update_complete_button_state()
    
    def _populate_efi_partitions(self):
        """Populate the EFI partition selection UI."""
        # Clear existing rows
        # In GTK 4, we need to remove rows differently
        for widget in list(self.efi_group):
            self.efi_group.remove(widget)
        
        efi_radio_group = None
        for i, efi_part in enumerate(self.efi_partitions):
            path = efi_part["path"]
            size_str = format_bytes(efi_part["size"]) if efi_part["size"] else "Unknown"
            
            row = Adw.ActionRow(
                title=f"EFI Partition: {path}",
                subtitle=f"Size: {size_str}, Type: {efi_part['fstype']}"
            )
            
            radio = Gtk.CheckButton() if i == 0 else Gtk.CheckButton(group=efi_radio_group)
            if i == 0:
                efi_radio_group = radio
                radio.set_active(True)  # Select first by default
                self.selected_efi_partition = path
                self.preserve_efi = True
            
            radio.set_valign(Gtk.Align.CENTER)
            radio.connect("toggled", self.on_efi_partition_selected, path)
            row.add_suffix(radio)
            row.set_activatable_widget(radio)
            self.efi_group.add(row)

    def find_physical_disk_for_path(self, target_path, block_devices):
        """Traces a given path back to its parent physical disk using lsblk data, handling loop devices."""
        print(f"--- Tracing physical disk for path: {target_path} ---")
        if not block_devices or not target_path:
            print("  Error: Missing block_devices or target_path.")
            return None

        # Create a mapping from any path to its device info and parent path (pkname)
        path_map = {}
        queue = list(block_devices)
        while queue:
            dev = queue.pop(0)
            dev_path = dev.get("path")
            if dev_path:
                path_map[dev_path] = {"info": dev, "pkname": dev.get("pkname")}
            if "children" in dev:
                queue.extend(dev["children"])

        # Trace upwards from the target_path
        current_path = target_path
        visited = set() # Prevent infinite loops

        while current_path and current_path not in visited:
            visited.add(current_path)
            print(f"  Tracing: current_path = {current_path}")

            # --- Handle Loop Device ---
            if current_path.startswith("/dev/loop"):
                print(f"  Path {current_path} is a loop device. Finding backing file...")
                try:
                    # Get Backing File path
                    cmd_losetup = ["losetup", "-O", "BACK-FILE", "--noheadings", current_path]
                    result_losetup = subprocess.run(cmd_losetup, capture_output=True, text=True, check=True, timeout=5)
                    backing_file = result_losetup.stdout.strip()
                    print(f"    Loop device {current_path} backing file: {backing_file}")

                    if backing_file and backing_file != "(deleted)": # Cannot trace deleted backing files reliably yet
                        backing_file_dir = os.path.dirname(backing_file)
                        print(f"    Finding mountpoint containing backing file directory: {backing_file_dir}...")

                        # Use findmnt to find the source device for the directory containing the backing file
                        # findmnt -n -o SOURCE --target /path/to/dir
                        cmd_findmnt_src = ["findmnt", "-n", "-o", "SOURCE", "--target", backing_file_dir]
                        result_findmnt_src = subprocess.run(cmd_findmnt_src, capture_output=True, text=True, check=True, timeout=5)
                        source_device = result_findmnt_src.stdout.strip()

                        if source_device:
                            print(f"    Backing file directory {backing_file_dir} is on source device: {source_device}")
                            current_path = source_device # Continue tracing from the source device
                            continue # Restart loop with the new source device path
                        else:
                            print(f"    ERROR: Could not find source device for backing file directory {backing_file_dir}")

                    print(f"    Trying lsblk parent (pkname) for loop device {current_path}...")
                    if current_path in path_map:
                         parent_path = path_map[current_path]["pkname"]
                         if parent_path:
                              print(f"    Found lsblk parent (pkname): {parent_path}. Continuing trace from parent.")
                              current_path = parent_path
                              continue # Restart loop with the parent device path
                         else:
                              print(f"    ERROR: Loop device {current_path} has no pkname in lsblk.")
                              return None
                    else:
                         # Should not happen if map was built correctly
                         print(f"    ERROR: Loop device {current_path} not found in path_map for pkname lookup.")
                         return None

                except subprocess.CalledProcessError as e:
                     print(f"  ERROR: Command failed while processing loop device {current_path}: {' '.join(e.cmd)}")
                     print(f"  Stderr: {e.stderr}")
                     print(f"  Continuing trace without resolving loop device further...") # Try to continue if command fails
                     # Let it fall through to the general path/pkname check below
                except Exception as e:
                    print(f"  ERROR: Failed to process loop device {current_path}: {e}")
                    return None # Critical error if something else goes wrong
            # --- End Handle Loop Device ---

            # --- Handle Device Mapper ---
            elif current_path.startswith("/dev/mapper/"):
                 print(f"  Path {current_path} is a device mapper device. Checking lsblk parent (pkname)...")
                 parent_path = path_map.get(current_path, {}).get("pkname")

                 if parent_path:
                      print(f"    Found lsblk parent (pkname): {parent_path}. Continuing trace from parent.")
                      current_path = parent_path
                      continue # Restart loop with parent path
                 else:
                      print(f"    Warning: Device mapper path {current_path} has no pkname in lsblk. Trying dmsetup...")
                      try:
                           cmd_dmsetup = ["dmsetup", "deps", "-o", "devname", current_path]
                           result_dmsetup = subprocess.run(cmd_dmsetup, capture_output=True, text=True, check=True, timeout=5)
                           # Output format: " device_name (major:minor)\n ..."
                           # We want the first device_name
                           deps_output = result_dmsetup.stdout.strip()
                           match = re.search(r"^\s*(\S+)", deps_output) # Find first non-whitespace sequence
                           if match:
                                underlying_dev = match.group(1)
                                # Ensure it's a device path
                                if underlying_dev.startswith("/dev/"):
                                     print(f"    Found underlying device via dmsetup: {underlying_dev}. Continuing trace.")
                                     current_path = underlying_dev
                                     continue # Restart loop with the underlying device
                                else:
                                     print(f"    Warning: dmsetup output '{underlying_dev}' doesn't look like a device path.")
                           else:
                                print(f"    Warning: Could not parse underlying device from dmsetup output: {deps_output}")
                      except FileNotFoundError:
                           print(f"    ERROR: dmsetup command not found. Cannot resolve DM dependency for {current_path}.")
                           return None # Cannot proceed without dmsetup if pkname missing
                      except subprocess.CalledProcessError as e:
                           print(f"    ERROR: dmsetup failed for {current_path}: {e.stderr}")
                           # Proceed to general check below? Might fail.
                      except Exception as e:
                           print(f"    ERROR: Unexpected error running dmsetup for {current_path}: {e}")
                           # Proceed to general check below? Might fail.

                      print(f"    Falling back to general check for {current_path} after dmsetup attempt.")
                      # If dmsetup fails or doesn't find a usable path, proceed to general check below

            # --- General Path Check ---
            if current_path not in path_map:
                print(f"  Error: Path {current_path} not found in lsblk map (needed for type/pkname check).")
                # It might have been resolved via dmsetup/losetup to a path not originally scanned
                # If we can't find it now, we cannot determine if it's a 'disk' or find its parent.
                return None


            dev_info = path_map[current_path]["info"]
            dev_type = dev_info.get("type")

            if dev_type == "disk":
                print(f"  Found parent disk: {current_path}")
                return current_path

            parent_path = path_map[current_path]["pkname"]
            if not parent_path:
                 print(f"  Error: Path {current_path} (type: {dev_type}) has no parent (pkname).")
                 # If it's a disk but type wasn't exactly 'disk', maybe return anyway?
                 if dev_type and "disk" in dev_type.lower():
                      print(f"  Treating path {current_path} as disk based on type '{dev_type}'.")
                      return current_path
                 return None # Cannot trace further without parent

            current_path = parent_path

        if current_path in visited: print(f"  Error: Loop detected while tracing parent for {target_path}")
        else: print(f"  Error: Could not find parent disk for {target_path} (trace ended unexpectedly)")
        return None

    def scan_for_disks(self, button):
        """Runs lsblk once, identifies the live OS disk, checks usage, and updates the UI."""
        print("Scanning for disks using lsblk...")
        button.set_sensitive(False)
        self.show_toast("Scanning for storage devices...")
        self.scan_completed = False
        self.partitioning_method = None
        self.selected_disks = set()
        self.disk_widgets = {}
        
        # Clear previous UI state
        self.disk_list_group.set_visible(False)
        self.mode_group.set_visible(False)
        self.fs_group.set_visible(False)
        self.efi_group.set_visible(False)
        self.layout_group.set_visible(False)
        self.layout_segments = []
        self.layout_selected = -1
        self._layout_disk = None
        self.complete_button.set_sensitive(False)
        
        # Reset radio buttons
        self.normal_radio.set_active(False)
        self.dual_boot_radio.set_active(False)

        try:
            # Run lsblk directly because disk discovery does not require privileges
            commands = [
                ["lsblk", "-J", "-b", "-p", "-o", disk_filter.LSBLK_COLUMNS],
                ["lsblk", "-J", "-b", "-p", "-o", "NAME,PATH,SIZE,MODEL,TYPE,PKNAME,MOUNTPOINT,TRAN,RO"],
                ["lsblk", "-J", "-b", "-p", "-o", "NAME,PATH,SIZE,MODEL,TYPE,PKNAME,MOUNTPOINT"],
            ]
            result = None
            errors = []
            for cmd in commands:
                print(f"Running: {' '.join(cmd)}")
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10
                )
                if result.returncode == 0:
                    break
                errors.append((result.stderr or result.stdout or "").strip())
            if result is None or result.returncode != 0:
                detail = next((item for item in reversed(errors) if item), "unknown lsblk failure")
                raise RuntimeError(f"lsblk failed: {detail}")
            lsblk_data = json.loads(result.stdout or "{}")
            
            self.detected_disks = []
            all_block_devices = lsblk_data.get("blockdevices", [])
            live_os_disk_path = None

            # --- Find the physical disk hosting the live OS root ('/') ---
            print("--- Searching for live OS root mountpoint ('/') ---")
            root_source_path = None
            queue = list(all_block_devices)
            processed_for_root = set() # Avoid reprocessing children
            while queue:
                 dev = queue.pop(0)
                 dev_path = dev.get("path")
                 if not dev_path or dev_path in processed_for_root: continue
                 processed_for_root.add(dev_path)

                 # Check current device
                 if dev.get("mountpoint") == "/":
                      root_source_path = dev_path
                      print(f"  Found root mountpoint '/' on device: {root_source_path}")
                      break # Found it

                 # Check children
                 if "children" in dev:
                      for child in dev["children"]:
                            child_path = child.get("path")
                            if child_path and child.get("mountpoint") == "/":
                                 root_source_path = child_path
                                 print(f"  Found root mountpoint '/' on child device: {root_source_path}")
                                 break # Found it
                            # Add grandchildren only if root not found yet
                            if "children" in child and root_source_path is None:
                                 queue.extend(child["children"])
                 if root_source_path: break # Exit outer loop if found

            if root_source_path:
                 live_os_disk_path = self.find_physical_disk_for_path(root_source_path, all_block_devices)
                 if live_os_disk_path:
                      print(f"--- Identified Live OS physical disk: {live_os_disk_path} ---")
                 else:
                      print("--- WARNING: Could not trace root mountpoint source back to a physical disk! ---")
            else:
                 print("--- WARNING: Could not find root mountpoint '/' in lsblk output! ---")
            # --- Finished searching for live OS disk ---

            # --- Process all detected physical disks ---
            live_medium = _live_medium_disks()
            if live_medium:
                print(f"Hiding live boot medium: {sorted(live_medium)}")
            print("--- Processing detected disks ---")
            for disk_info in disk_filter.installable_disks(all_block_devices, live_medium):
                disk_info["is_live_os_disk"] = disk_info["path"] == live_os_disk_path
                print(f"  Processing disk: {disk_info['path']}, Is Live OS Disk? {disk_info['is_live_os_disk']}, Portable? {disk_info['portable']}")
                self.detected_disks.append(disk_info)

            print(f"Detected disks list: {self.detected_disks}")
            self._populate_disk_list()
            self.scan_completed = True
            self.show_toast(f"Scan complete. Found {len(self.detected_disks)} disk(s).")
            
            if self.detected_disks:
                self.disk_list_group.set_visible(True)
                self.mode_group.set_visible(True)
                self.fs_group.set_visible(True)
                self._check_dual_boot_available()
                self.normal_radio.set_active(True)  # Default to normal install
                # Default root filesystem to ext4 on each successful scan
                self.fs_row.set_selected(0)
                self.on_filesystem_changed(self.fs_row, None)
            else:
                 self.show_toast("No suitable disks found for installation.")

        except FileNotFoundError:
            print("ERROR: lsblk command not found.")
            self.show_toast("Error: lsblk command not found. Cannot scan disks.")
        except subprocess.CalledProcessError as e:
            print(f"ERROR: lsblk failed: {e}")
            print(f"Stderr: {e.stderr}")
            self.show_toast(f"Error running lsblk: {e.stderr}")
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse lsblk JSON output: {e}")
            self.show_toast("Error parsing disk information.")
        except subprocess.TimeoutExpired:
            print("ERROR: lsblk command timed out.")
            self.show_toast("Disk scan timed out.")
        except Exception as e:
            print(f"ERROR: Unexpected error during disk scan: {e}")
            self.show_toast(f"An unexpected error occurred during disk scan.")
        finally:
            # Re-enable scan button regardless of outcome
            button.set_sensitive(True)
            self.update_complete_button_state()
            
    def _populate_disk_list(self):
        """Populate the disk list with detected disks."""
        # Remove previously added rows (AdwPreferencesGroup iterates internal structure; we track our own)
        for row in self.disk_list_rows:
            row.unparent()
        self.disk_list_rows = []
        self.disk_widgets = {}

        if not self.detected_disks:
            row = Adw.ActionRow(
                title="No suitable disks found", 
                subtitle="Cannot proceed with installation."
            )
            row.set_activatable(False)
            self.disk_list_group.add(row)
            self.disk_list_rows.append(row)
            return

        disk_radio_group = None
        found_usable_disk = False
        default_path = disk_filter.default_disk(self.detected_disks)
        
        for i, disk in enumerate(self.detected_disks):
            disk_path = disk["path"]
            disk_size_str = format_bytes(disk["size"])
            title = f"{disk['model']} ({disk_path})"
            subtitle = f"Size: {disk_size_str}"
            if disk.get("portable"):
                subtitle += " (Removable)"
            
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            
            if disk["is_live_os_disk"]:
                 print(f"!!! UI Update: Marking {disk['path']} (Live OS Disk) as insensitive.")
                 row.set_subtitle(subtitle + " (Live OS Disk - Cannot select)")
                 row.set_sensitive(False)
                 warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                 warning_icon.set_tooltip_text("This disk contains the live operating system")
                 row.add_suffix(warning_icon)
            else:
                 found_usable_disk = True
                 radio = Gtk.CheckButton() if disk_radio_group is None else Gtk.CheckButton(group=disk_radio_group)
                 if disk_radio_group is None:
                     disk_radio_group = radio
                 if disk_path == default_path:
                     radio.set_active(True)
                     self.selected_disks.add(disk_path)
                 
                 radio.set_valign(Gtk.Align.CENTER)
                 radio.connect("toggled", self.on_disk_toggled, disk_path)
                 row.add_suffix(radio)
                 row.set_activatable_widget(radio)
                 self.disk_widgets[disk_path] = {"row": row, "radio": radio}

            self.disk_list_group.add(row)
            self.disk_list_rows.append(row)
            
        if not found_usable_disk:
             print("Warning: No usable disks detected (only Live OS disk found?).")
        elif default_path is None:
             print("No internal disk found. Waiting for the user to pick a disk.")

    def on_disk_toggled(self, radio_button, disk_path):
        """Handle disk selection toggle."""
        print(f"--- Toggle event for {disk_path} ---")
        
        if radio_button.get_active():
            print(f"  Adding {disk_path} to selected_disks.")
            self.selected_disks.clear()  # Only one disk at a time for now
            self.selected_disks.add(disk_path)
            if self.dual_boot_enabled:
                self._refresh_layout()
        
        self.update_complete_button_state()

    def update_complete_button_state(self):
        """Update the state of the complete button based on current selections."""
        print(f"--- Updating button state ---")
        print(f"  Selected disks: {self.selected_disks}")
        print(f"  Partitioning method: {self.partitioning_method}")
        
        selected_disk = next(iter(self.selected_disks), None) if self.selected_disks else None
        dual_boot_ok = (
            selected_disk in self.disks_with_free_space and
            self.selected_efi_partition is not None and
            self._selected_segment() is not None
        )
        can_proceed = (
            self.scan_completed and 
            len(self.selected_disks) > 0 and 
            self.partitioning_method is not None and
            (not self.dual_boot_enabled or dual_boot_ok)
        )
        
        print(f"  Setting Complete button sensitive: {can_proceed}")
        self.complete_button.set_sensitive(can_proceed)
        
    def apply_settings_and_return(self, button):
        """Apply the storage configuration and return to summary."""
        print(f"--- Apply Settings START ---")
        print(f"  Selected disks: {self.selected_disks}")
        print(f"  Installation mode: {self.partitioning_method}")
        print(f"  Filesystem: {self.filesystem_type}")
        print(f"  Dual boot: {self.dual_boot_enabled}")
        print(f"  Preserve EFI: {self.preserve_efi}")
        
        # Re-validate conditions before proceeding
        self.update_complete_button_state()
        if not self.complete_button.get_sensitive():
             self.show_toast("Please complete all required selections.")
             return

        if not self.selected_disks:
             self.show_toast("Please select a disk for installation.")
             return

        primary_disk = sorted(list(self.selected_disks))[0]
        
        # Initialize config_values
        btrfs_subvolumes = (
            self.filesystem_type == "btrfs"
            and self.btrfs_snapshots_row.get_sensitive()
            and getattr(self, "btrfs_snapshots_enabled", False)
        )
        config_values = {
            "method": self.partitioning_method,
            "target_disks": sorted(list(self.selected_disks)), 
            "filesystem": self.filesystem_type,
            "btrfs_subvolumes": btrfs_subvolumes,
            "dual_boot": self.dual_boot_enabled,
            "preserve_efi": self.preserve_efi,
            "selected_efi_partition": self.selected_efi_partition,
            "custom_format": self.custom_format_enabled,
            "commands": [],
            "partitions": []
        }

        if self.partitioning_method in ["normal", "dual_boot"]:
            print(f"  Generating partitioning commands for: {primary_disk}")
            is_uefi = os.path.exists("/sys/firmware/efi")
            efi_size = int(self.efi_size_row.get_value()) if self.custom_format_enabled else 512
            commands = []
            partitions = []
            boot_part = None

            if self.dual_boot_enabled:
                if not is_uefi:
                    self.show_toast("Dual boot requires UEFI firmware.")
                    return
                if not self.selected_efi_partition:
                    self.show_toast("Select an existing EFI partition for dual boot.")
                    return
                layout = get_disk_layout(primary_disk)
                chosen = self._selected_segment()
                if chosen and self._layout_disk == primary_disk:
                    chosen = {_segment_key(s): s for s in layout["segments"]}.get(_segment_key(chosen))
                else:
                    chosen = None
                if not chosen or not chosen["selectable"]:
                    self._refresh_layout()
                    self.show_toast(f"Dual boot needs free space (at least {MIN_FREE_MIB // 1024} GiB) or an unused partition (at least 9 GiB). Select one in the disk layout.")
                    return
                partitions.append({
                    "device": self.selected_efi_partition,
                    "mountpoint": "/boot/efi",
                    "fstype": "vfat"
                })
                if chosen["kind"] == "part":
                    root_part = chosen["path"]
                    expect_devices = [root_part]
                else:
                    boot_part = get_next_partition_device(primary_disk)
                    if not boot_part or os.path.exists(boot_part):
                        self.show_toast("Could not determine a free partition number for dual boot.")
                        return
                    boot_num = int(re.search(r"(\d+)$", boot_part).group(1))
                    root_part = get_next_partition_device(primary_disk, boot_num)
                    if not root_part or os.path.exists(root_part):
                        self.show_toast("Could not determine a free partition number for dual boot.")
                        return
                    root_num = int(re.search(r"(\d+)$", root_part).group(1))
                    start_mib = chosen["start_mib"]
                    end_mib = chosen["end_mib"]
                    boot_end_mib = start_mib + BOOT_SIZE_MIB
                    commands.extend([
                        _dual_boot_sfdisk_append(primary_disk, boot_num, root_num, start_mib, boot_end_mib, end_mib),
                        ["partprobe", primary_disk],
                        ["udevadm", "settle", "--timeout=30"],
                        ["mkfs.ext4", "-F", boot_part],
                    ])
                    partitions.append({"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"})
                    expect_devices = [boot_part, root_part]
            else:
                boot_part = _part(primary_disk, 2)
                root_part = _part(primary_disk, 3)
                if is_uefi:
                    partitions.append({"device": _part(primary_disk, 1), "mountpoint": "/boot/efi", "fstype": "vfat"})
                commands.extend([
                    ["wipefs", "-af", primary_disk],
                    _sfdisk_cmd(primary_disk, _clean_install_sfdisk_script(is_uefi, efi_size)),
                    ["partprobe", primary_disk],
                    ["udevadm", "settle", "--timeout=30"],
                ])
                if is_uefi:
                    commands.append(["mkfs.fat", "-F32", _part(primary_disk, 1)])
                commands.append(["mkfs.ext4", "-F", boot_part])
                partitions.append({"device": boot_part, "mountpoint": "/boot", "fstype": "ext4"})
                expect_devices = [_part(primary_disk, n) for n in (1, 2, 3)]

            commands.append(_mkfs_root_command(self.filesystem_type, root_part))
            partitions.append({"device": root_part, "mountpoint": "/", "fstype": self.filesystem_type})

            config_values["commands"] = commands
            config_values["partitions"] = partitions
            config_values["separate_boot"] = bool(boot_part)
            config_values["expect_devices"] = expect_devices

            print("=== FINAL COMMANDS LIST ===")
            for i, cmd in enumerate(commands):
                print(f"Command {i+1}: {' '.join(shlex.quote(c) for c in cmd)}")
            print(f"Partitions: {partitions}")

        print("Storage configuration confirmed. Returning to summary.")
        
        mode_text = "Dual boot" if self.dual_boot_enabled else "Clean installation"
        self.show_toast(f"{mode_text} on {primary_disk} with {self.filesystem_type} filesystem")
        
        super().mark_complete_and_return(button, config_values=config_values) 
