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
# centrio_installer/utils.py


import os
import platform

# Attempt D-Bus import
try:
    # Use dasbus
    import dasbus.connection
    from dasbus.error import DBusError
    dbus_available = True
except ImportError:
    dasbus = None 
    DBusError = Exception # Placeholder
    dbus_available = False
    print("WARNING: dasbus library not found. D-Bus communication will be disabled.")

# Note: Avoid importing GUI or app-specific constants here to keep utils lightweight.

def get_host_architecture():
    """Return architecture-specific bootloader and package names.
    Supports x86_64 and aarch64 (ARM64). Returns dict with keys:
    efi_suffix, efi_shim, efi_grub, efi_boot, grub_efi_pkg, grub_efi_modules_pkg,
    shim_pkg, has_bios (grub2-pc for legacy BIOS; False on ARM64).
    """
    mach = platform.machine().lower()
    if mach in ("x86_64", "amd64"):
        return {
            "arch": "x86_64",
            "efi_suffix": "x64",
            "efi_shim": "shimx64.efi",
            "efi_grub": "grubx64.efi",
            "efi_boot": "BOOTX64.EFI",
            "grub_efi_pkg": "grub2-efi-x64",
            "grub_efi_modules_pkg": "grub2-efi-x64-modules",
            "shim_pkg": "shim-x64",
            "has_bios": True,
        }
    if mach in ("aarch64", "arm64"):
        return {
            "arch": "aarch64",
            "efi_suffix": "aa64",
            "efi_shim": "shimaa64.efi",
            "efi_grub": "grubaa64.efi",
            "efi_boot": "BOOTAA64.EFI",
            "grub_efi_pkg": "grub2-efi-aa64",
            "grub_efi_modules_pkg": "grub2-efi-aa64-modules",
            "shim_pkg": "shim-aa64",
            "has_bios": False,
        }
    # Fallback: treat as x86_64 for unknown arch (may fail)
    print(f"Warning: Unsupported architecture {mach}, defaulting to x86_64 packages")
    return {
        "arch": mach,
        "efi_suffix": "x64",
        "efi_shim": "shimx64.efi",
        "efi_grub": "grubx64.efi",
        "efi_boot": "BOOTX64.EFI",
        "grub_efi_pkg": "grub2-efi-x64",
        "grub_efi_modules_pkg": "grub2-efi-x64-modules",
        "shim_pkg": "shim-x64",
        "has_bios": True,
    }


def get_os_release_info(target_root=None):
    """Parses /etc/os-release (or /usr/lib/os-release) to get NAME and VERSION_ID.
    If target_root is provided, reads from within that root.
    """
    info = {"NAME": "Linux", "VERSION": None, "VERSION_ID": None, "ID": None} # Defaults
    release_file_path = None
    base_path = target_root if target_root else "/"
    
    # Check standard locations relative to base_path
    etc_path = os.path.join(base_path, "etc/os-release")
    usr_lib_path = os.path.join(base_path, "usr/lib/os-release")
    
    if os.path.exists(etc_path):
        release_file_path = etc_path
    elif os.path.exists(usr_lib_path):
        release_file_path = usr_lib_path
    
    if release_file_path:
        try:
            with open(release_file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        # Remove quotes from value if present
                        value = value.strip('"\'') 
                        # Store common keys (include VERSION for nicer display)
                        if key in ["NAME", "VERSION", "VERSION_ID", "ID"]:
                            info[key] = value
        except Exception as e:
            print(f"Warning: Failed to parse {release_file_path}: {e}")
            
    return info

# Function to get Anaconda bus address (Modified)
def get_anaconda_bus_address():
    # This function likely contained D-Bus logic to find the Anaconda bus.
    # As D-Bus is removed/optional, provide a placeholder.
    print("Warning: get_anaconda_bus_address() is not implemented (D-Bus disabled/removed).")
    pass # Add pass to make the function definition valid
    # // ... existing code ... # This comment is likely outdated now

# Constants
# ANACONDA_BUS_NAME = "org.fedoraproject.Anaconda.Boss"
# ANACONDA_OBJECT_PATH = "/org/fedoraproject/Anaconda/Boss" 