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

# --- Enhanced Partitioning Command Generators ---

def generate_wipefs_command(disk_path):
    """Generates the wipefs command for a disk."""
    return ["wipefs", "-a", disk_path]

def generate_gpt_commands(disk_path, efi_size_mb=512, filesystem="btrfs", dual_boot=False, preserve_efi=False, bios_mode=False):
    """Generates parted commands for GPT layout.
    - UEFI (bios_mode=False): creates EFI System Partition + root
    - BIOS (bios_mode=True): creates BIOS Boot Partition + root
    """
    commands = []
    if not disk_path:
        print("ERROR: generate_gpt_commands called without disk_path")
        return []

    # Define partition start and end points
    first_start = "1MiB"
    if bios_mode:
        bios_end = "3MiB"  # ~2MiB bios_grub region is sufficient
    else:
        efi_end = f"{efi_size_mb + 1}MiB"
    
    # Layout decisions
    if bios_mode:
        # BIOS on GPT requires a bios_grub partition for core.img embedding
        # Make it 4 MiB to satisfy alignment and tool thresholds
        root_start = "4MiB"
        root_end = "100%"
        commands.append(["parted", "-s", disk_path, "mklabel", "gpt"])
        commands.append(["parted", "-s", disk_path, "mkpart", "\"BIOS boot\"", "", first_start, bios_end])
        commands.append(["parted", "-s", disk_path, "set", "1", "bios_grub", "on"])
        commands.append(["parted", "-s", disk_path, "mkpart", "\"Linux filesystem\"", filesystem, root_start, root_end])
    else:
        if dual_boot and preserve_efi:
            # Create partition in actual free space (user must have unallocated space)
            region = get_free_space_region(disk_path)
            if not region:
                print("ERROR: Dual boot requires free space but none found on disk.")
                return []
            root_start, root_end = region
            commands.append(["parted", "-s", disk_path, "mkpart", "\"Linux filesystem\"", filesystem, root_start, root_end])
        else:
            # Normal UEFI installation - create full layout
            root_start = efi_end
            root_end = "100%"
            commands.append(["parted", "-s", disk_path, "mklabel", "gpt"])
            commands.append(["parted", "-s", disk_path, "mkpart", "\"EFI System Partition\"", "fat32", first_start, efi_end])
            commands.append(["parted", "-s", disk_path, "set", "1", "boot", "on"])
            commands.append(["parted", "-s", disk_path, "set", "1", "esp", "on"])
            commands.append(["parted", "-s", disk_path, "mkpart", "\"Linux filesystem\"", filesystem, root_start, root_end])
    
    return commands

def generate_mkfs_commands(disk_path, filesystem="btrfs", partition_prefix="", dual_boot=False, preserve_efi=False, include_efi=True, bios_mode=False, root_part_override=None):
    """Generates mkfs commands for partitions.
    - include_efi=True: partition 1 is EFI (vfat), partition 2 is root
    - dual_boot+preserve_efi: use root_part_override for the new partition (part N+1)
    """
    commands = []
    if root_part_override:
        root_part = root_part_override
    elif bios_mode:
        root_part = f"{disk_path}{partition_prefix}2"
    elif include_efi:
        efi_part = f"{disk_path}{partition_prefix}1"
        root_part = f"{disk_path}{partition_prefix}2"
        if not (dual_boot and preserve_efi):
            commands.append(["mkfs.vfat", "-F32", efi_part])
    else:
        root_part = f"{disk_path}{partition_prefix}1"

    # Format root partition with selected filesystem
    if filesystem == "ext4":
        commands.append(["mkfs.ext4", "-F", root_part])
    elif filesystem == "btrfs":
        commands.append(["mkfs.btrfs", "-f", root_part])
    elif filesystem == "xfs":
        commands.append(["mkfs.xfs", "-f", root_part])
    else:
        raise ValueError(f"Unsupported filesystem: {filesystem}. Use ext4, btrfs, or xfs.")
    return commands

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
    """Check if a disk has unallocated (free) space for dual boot. Returns True if yes."""
    return get_free_space_region(disk_path) is not None


def get_free_space_region(disk_path):
    """Get the (start, end) of the largest free space region on disk for dual boot.
    Returns (start_str, end_str) e.g. ('256GiB', '500GiB') or None if no free space."""
    if not disk_path or not os.path.exists(disk_path):
        return None
    try:
        r = subprocess.run(
            ["parted", "-s", disk_path, "unit", "MiB", "print", "free"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return None
        # Parse lines like "       262144MiB  512000MiB  249856MiB   Free Space"
        lines = r.stdout.strip().split("\n")
        best_start, best_end, best_size_mb = None, None, 0
        for line in lines:
            if "Free Space" not in line and "free" not in line.lower():
                continue
            parts = line.split()
            if len(parts) >= 3:
                start_s, end_s, size_s = parts[0], parts[1], parts[2]
                try:
                    s = size_s.upper().replace("GIB", "").replace("MIB", "").replace("KB", "").replace("B", "").strip()
                    num = float(s) if s else 0
                    if "GIB" in size_s.upper() or "GB" in size_s.upper():
                        num *= 1024  # to MiB
                    if num > best_size_mb and num > 100:  # at least 100 MiB
                        best_start, best_end, best_size_mb = start_s, end_s, num
                except (ValueError, IndexError):
                    pass
        if best_start and best_end:
            return (best_start, best_end)
        return None
    except Exception:
        return None


def get_next_partition_device(disk_path, partition_prefix=""):
    """Return the device path for the next partition to be created (e.g. /dev/sda3).
    Used for dual boot where we add one partition to existing layout."""
    if not disk_path:
        return None
    try:
        r = subprocess.run(
            ["lsblk", "-n", "-o", "NAME", "-l", disk_path],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return None
        max_num = 0
        base = disk_path.split("/")[-1]  # e.g. sda or nvme0n1
        for line in r.stdout.strip().split("\n"):
            name = line.strip()
            if not name or name == base:
                continue
            # sda1, sda2 or nvme0n1p1, nvme0n1p2
            suffix = name[len(base):].lstrip("p")
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
        next_num = max_num + 1
        if "nvme" in disk_path or "mmcblk" in disk_path:
            return f"{disk_path}p{next_num}"
        return f"{disk_path}{next_num}"
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


def detect_existing_efi_partitions():
    """Detect existing EFI system partitions that could be reused for dual boot."""
    efi_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    efi_partitions = []
    seen_paths = set()
    try:
        # Fallback: if /boot/efi is mounted, use that partition
        ok_fm, _, out_fm = backend._run_command(
            ["findmnt", "-n", "-o", "SOURCE", "/boot/efi"],
            "Find EFI mount", timeout=5
        )
        if ok_fm and out_fm and out_fm.strip():
            src = out_fm.strip()
            if src and src not in seen_paths:
                seen_paths.add(src)
                efi_partitions.append({"path": src, "size": None, "fstype": "vfat"})

        cmd = ["lsblk", "-J", "-o", "PATH,FSTYPE,PARTTYPE,SIZE"]
        ok, _, stdout = backend._run_command(cmd, "List block devices for EFI", timeout=10)
        if not ok:
            raise RuntimeError("lsblk failed")
        lsblk_data = json.loads(stdout or "{}")

        def scan_device(device):
            path = device.get("path")
            fstype = device.get("fstype")
            parttype = (device.get("parttype") or "").lower()
            size = device.get("size")
            if not path or path in seen_paths:
                return
            # EFI GUID (case-insensitive); also accept vfat first partition on GPT
            is_efi = (
                fstype == "vfat" and parttype == efi_guid
            ) or (
                fstype == "vfat" and "efi" in (path or "").lower()
            )
            if is_efi:
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
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(title="Installation Destination", subtitle="Configure disk partitioning and filesystem", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        
        # State variables
        self.detected_disks = []
        self.selected_disks = set()
        self.scan_completed = False
        self.partitioning_method = None
        self.filesystem_type = "btrfs"
        self.dual_boot_enabled = False
        self.preserve_efi = False
        self.selected_efi_partition = None
        self.custom_format_enabled = False
        self.disk_widgets = {}
        self.disk_list_rows = []  # Track rows for proper cleanup on rescan
        self.efi_partitions = []
        self.disks_with_free_space = set()
        
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
        fs_model.append("ext4")
        fs_model.append("btrfs (default)")
        fs_model.append("xfs")
        fs_row.set_model(fs_model)
        fs_row.set_selected(1)  # Default to btrfs
        fs_row.connect("notify::selected", self.on_filesystem_changed)
        self.fs_group.add(fs_row)
        
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
        self.efi_partitions = detect_existing_efi_partitions()
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
                if self.efi_partitions and not self.selected_efi_partition:
                    self.selected_efi_partition = self.efi_partitions[0].get("path")
                    self.preserve_efi = True
                print(f"Installation mode: Dual boot (found {len(self.efi_partitions)} EFI partitions)")
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
        print(f"Selected filesystem: {self.filesystem_type}")
        self.update_complete_button_state()
    
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
                row.set_subtitle(f"{size_str} — has free space")
            else:
                row.set_sensitive(False)
                row.set_subtitle(f"{size_str} — no free space (shrink a partition first)")
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
        self.complete_button.set_sensitive(False)
        
        # Reset radio buttons
        self.normal_radio.set_active(False)
        self.dual_boot_radio.set_active(False)

        try:
            # Run lsblk ONCE, get JSON tree, include MOUNTPOINT (use backend for sudo when not root)
            cmd = ["lsblk", "-J", "-b", "-p", "-o", "NAME,PATH,SIZE,MODEL,TYPE,PKNAME,MOUNTPOINT,TRAN"]
            print(f"Running: {' '.join(cmd)}")
            ok, err, stdout = backend._run_command(cmd, "Scan block devices", timeout=10)
            if not ok:
                raise subprocess.CalledProcessError(1, cmd, err or "")
            lsblk_data = json.loads(stdout or "{}")
            
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
            print("--- Processing detected disks ---")
            for device in all_block_devices:
                # Skip optical, USB/portable drives
                tran = (device.get("tran") or "").lower()
                if tran == "usb":
                    print(f"  Skipping USB/portable disk: {device.get('path')}")
                    continue
                if device.get("type") == "disk" and not any(s in (device.get("model") or "").upper() for s in ["CD", "DVD"]):
                    disk_path = device.get("path")
                    if not disk_path: continue

                    # Mark disk as unusable only if it's the one hosting the live OS
                    is_live_os_disk = (disk_path == live_os_disk_path)
                    
                    print(f"  Processing disk: {disk_path}, Is Live OS Disk? {is_live_os_disk}")

                    disk_info = {
                        "name": device.get("name") or "N/A",
                        "path": disk_path,
                        "size": device.get("size"),
                        "model": (device.get("model") or "Unknown Model").strip(),
                        "is_live_os_disk": is_live_os_disk # Changed flag name
                    }
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
        
        for i, disk in enumerate(self.detected_disks):
            disk_path = disk["path"]
            disk_size_str = format_bytes(disk["size"])
            title = f"{disk['model']} ({disk_path})"
            subtitle = f"Size: {disk_size_str}"
            
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
                     radio.set_active(True)  # Select first usable disk by default
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

    def on_disk_toggled(self, radio_button, disk_path):
        """Handle disk selection toggle."""
        print(f"--- Toggle event for {disk_path} ---")
        
        if radio_button.get_active():
            print(f"  Adding {disk_path} to selected_disks.")
            self.selected_disks.clear()  # Only one disk at a time for now
            self.selected_disks.add(disk_path)
        
        self.update_complete_button_state()

    def update_complete_button_state(self):
        """Update the state of the complete button based on current selections."""
        print(f"--- Updating button state ---")
        print(f"  Selected disks: {self.selected_disks}")
        print(f"  Partitioning method: {self.partitioning_method}")
        
        selected_disk = next(iter(self.selected_disks), None) if self.selected_disks else None
        dual_boot_ok = (
            selected_disk in self.disks_with_free_space and
            self.selected_efi_partition is not None
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
        config_values = {
            "method": self.partitioning_method,
            "target_disks": sorted(list(self.selected_disks)), 
            "filesystem": self.filesystem_type,
            "dual_boot": self.dual_boot_enabled,
            "preserve_efi": self.preserve_efi,
            "selected_efi_partition": self.selected_efi_partition,
            "custom_format": self.custom_format_enabled,
            "commands": [],
            "partitions": []
        }

        if self.partitioning_method in ["normal", "dual_boot"]:
            print(f"  Generating partitioning commands for: {primary_disk}")
            
            # Detect firmware type to decide partition layout
            is_uefi = os.path.exists("/sys/firmware/efi")

            # Get EFI size if custom formatting is enabled
            efi_size = int(self.efi_size_row.get_value()) if self.custom_format_enabled else 512
            
            # Generate the command lists
            partition_prefix = "p" if "nvme" in primary_disk else ""
            
            print(f"=== DISK CONFIGURATION DEBUG ===")
            print(f"Primary disk: {primary_disk}")
            print(f"Partition prefix: '{partition_prefix}'")
            print(f"EFI size: {efi_size} MB")
            print(f"Filesystem: {self.filesystem_type}")
            print(f"Dual boot: {self.dual_boot_enabled}")
            print(f"Preserve EFI: {self.preserve_efi}")
            print(f"=== GENERATING COMMANDS ===")
            
            if not (self.dual_boot_enabled and self.preserve_efi):
                wipe_cmd = generate_wipefs_command(primary_disk)
                config_values["commands"].append(wipe_cmd)
                print(f"Wipe command: {wipe_cmd}")
            
            parted_cmds = generate_gpt_commands(
                primary_disk,
                efi_size_mb=efi_size,
                filesystem=self.filesystem_type,
                dual_boot=self.dual_boot_enabled,
                preserve_efi=self.preserve_efi if is_uefi else False,
                bios_mode=not is_uefi
            )
            config_values["commands"].extend(parted_cmds)
            print(f"Parted commands: {parted_cmds}")
            
            include_efi = is_uefi and not (self.dual_boot_enabled and self.preserve_efi)
            root_part_override = None
            if self.dual_boot_enabled and self.preserve_efi:
                root_part_override = get_next_partition_device(primary_disk, partition_prefix)
                if not root_part_override:
                    self.show_toast("Could not determine new partition device for dual boot.")
                    return
            mkfs_cmds = generate_mkfs_commands(
                primary_disk,
                filesystem=self.filesystem_type,
                partition_prefix=partition_prefix,
                dual_boot=self.dual_boot_enabled,
                preserve_efi=self.preserve_efi if is_uefi else False,
                include_efi=include_efi,
                bios_mode=not is_uefi,
                root_part_override=root_part_override
            )
            config_values["commands"].extend(mkfs_cmds)
            print(f"Mkfs commands: {mkfs_cmds}")
            
            # Define partition layout
            part1_suffix = f"{partition_prefix}1"
            part2_suffix = f"{partition_prefix}2"
            
            print(f"=== PARTITION LAYOUT ===")
            print(f"Part1 suffix: '{part1_suffix}'")
            print(f"Part2 suffix: '{part2_suffix}'")
            
            partitions = []
            if is_uefi:
                if not (self.dual_boot_enabled and self.preserve_efi):
                    efi_device = f"{primary_disk}{part1_suffix}"
                    partitions.append({
                        "device": efi_device,
                        "mountpoint": "/boot/efi",
                        "fstype": "vfat"
                    })
                    print(f"EFI partition: device={efi_device}, mountpoint=/boot/efi, fstype=vfat")
                    root_device = f"{primary_disk}{part2_suffix}"
                elif self.selected_efi_partition:
                    partitions.append({
                        "device": self.selected_efi_partition,
                        "mountpoint": "/boot/efi",
                        "fstype": "vfat"
                    })
                    print(f"Using existing EFI partition: device={self.selected_efi_partition}")
                    root_device = root_part_override or f"{primary_disk}{partition_prefix}2"
                else:
                    root_device = root_part_override or f"{primary_disk}{part2_suffix}"
            else:
                # BIOS (GPT): partition 1 is bios_grub, partition 2 is root
                root_device = f"{primary_disk}{part2_suffix}"
            partitions.append({
                "device": root_device, 
                "mountpoint": "/", 
                "fstype": self.filesystem_type
            })
            print(f"Root partition: device={root_device}, mountpoint=/, fstype={self.filesystem_type}")
            
            config_values["partitions"] = partitions
            
            print(f"=== FINAL COMMANDS LIST ===")
            for i, cmd in enumerate(config_values["commands"]):
                print(f"Command {i+1}: {' '.join(cmd)}")
            print(f"=== END DISK CONFIGURATION DEBUG ===")
            
            if config_values["commands"]:
                 print(f"    Example command: {' '.join(shlex.quote(c) for c in config_values['commands'][0])}")

        print("Storage configuration confirmed. Returning to summary.")
        
        mode_text = "Dual boot" if self.dual_boot_enabled else "Clean installation"
        self.show_toast(f"{mode_text} on {primary_disk} with {self.filesystem_type} filesystem")
        
        super().mark_complete_and_return(button, config_values=config_values) 