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
import re
import subprocess

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

# --- Timezone Helpers ---
def _get_timezone_list():
    """Return full IANA timezone list. Requires zoneinfo (Python 3.9+)."""
    try:
        from zoneinfo import available_timezones
    except ImportError:
        raise RuntimeError("zoneinfo is required for timezones (Python 3.9+).")
    zones = sorted(available_timezones())
    if not zones:
        raise RuntimeError("zoneinfo.available_timezones() returned no timezones.")
    print(f"  Loaded {len(zones)} timezones from zoneinfo.")
    return zones


def ana_get_all_regions_and_timezones():
    """Return full list of IANA timezone identifiers for the timezone selector."""
    return _get_timezone_list()

def _parse_xkb_layout_descriptions():
    """Parse /usr/share/X11/xkb/rules/evdev.lst for layout code -> human-readable name."""
    desc = {}
    path = "/usr/share/X11/xkb/rules/evdev.lst"
    if not os.path.exists(path):
        return desc
    try:
        in_layout = False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.strip() == "! layout":
                    in_layout = True
                    continue
                if in_layout:
                    if line.startswith("!"):
                        break
                    # Format: "  us              English (US)"
                    parts = line.split(None, 1)
                    if len(parts) >= 2:
                        code, name = parts[0], parts[1]
                        desc[code] = name
        print(f"  Loaded {len(desc)} keyboard layout descriptions from evdev.lst.")
    except Exception as e:
        print(f"  Could not parse evdev.lst: {e}")
    return desc


def _console_keymap_display_name(code, descriptions):
    """Map a localectl keymap name to a readable label using XKB layout names.

    list-keymaps mixes hundreds of hardware-specific and variant names. Showing
    raw codes looks like gibberish. We only attach a friendly name when we can
    derive it from an evdev ! layout entry, optionally with a variant suffix.
    """
    if not code or not descriptions:
        return None
    if code in descriptions:
        return descriptions[code]
    parts = code.split("-")
    for i in range(len(parts), 0, -1):
        prefix = "-".join(parts[:i])
        if prefix in descriptions:
            if i == len(parts):
                return descriptions[prefix]
            suffix = "-".join(parts[i:])
            return f"{descriptions[prefix]} ({suffix})"
    return None


def ana_get_keyboard_layouts():
    """Return (display_name, keymap_code) for layouts usable with localectl.

    Entries are limited to names we can label from XKB evdev.lst, intersected
    with localectl list-keymaps, plus common compound maps that share an evdev
    layout prefix (de-latin1-nodeadkeys -> German (...)).
    """
    print("Fetching keyboard layouts using localectl...")
    descriptions = _parse_xkb_layout_descriptions()
    try:
        env = os.environ.copy()
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("LANG", "C.UTF-8")
        result = subprocess.run(
            ["localectl", "list-keymaps"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        keymaps_set = {line.strip() for line in result.stdout.split("\n") if line.strip()}
        if not keymaps_set:
            keymaps_set = {"us"}

        pairs = []
        if descriptions:
            for code in sorted(keymaps_set, key=str.lower):
                label = _console_keymap_display_name(code, descriptions)
                if label is None:
                    continue
                pairs.append((label, code))
            pairs.sort(key=lambda x: (x[0].lower(), x[1].lower()))
        if not pairs:
            # No evdev.lst (or no overlap): keep a minimal safe list
            for fallback in ("us", "uk", "de", "fr"):
                if fallback in keymaps_set:
                    pairs.append((fallback, fallback))
            if not pairs and keymaps_set:
                fb = sorted(keymaps_set, key=str.lower)[0]
                pairs.append((fb, fb))
        print(f"  Found {len(pairs)} keyboard layouts (filtered for readable names).")
        return pairs
    except FileNotFoundError:
        raise RuntimeError("localectl is required for keyboard layouts. Install systemd or ensure localectl is in PATH.")
    except (subprocess.CalledProcessError, Exception) as e:
        raise RuntimeError(f"localectl list-keymaps failed: {e}") from e

# One codeset segment (may contain hyphens, e.g. UTF-8, ISO-8859-15); optional @modifier.
_LOCALE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9_.@-]+$")


def ana_get_available_locales():
    """Fetch locale identifiers from localectl as a sorted list.

    We show the raw identifier in the UI. Guessing \"English (US)\" from codes
    breaks on modifiers and non-ASCII output, and wrong subprocess decoding
    turns lines into gibberish. Forcing UTF-8 in the child and accepting only
    sane ASCII-looking lines avoids that.
    """
    print("Fetching available locales using localectl...")
    try:
        env = os.environ.copy()
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("LANG", "C.UTF-8")
        result = subprocess.run(
            ["localectl", "list-locales"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        seen = set()
        codes = []
        for line in result.stdout.splitlines():
            s = line.strip()
            if not s or "." not in s:
                continue
            if not _LOCALE_ID_RE.fullmatch(s):
                continue
            if s in seen:
                continue
            seen.add(s)
            codes.append(s)
        codes.sort(key=str.lower)
        print(f"  Found {len(codes)} locales.")
        if not codes:
            raise RuntimeError("localectl list-locales returned no usable locale lines.")
        return codes

    except FileNotFoundError:
        raise RuntimeError("localectl is required for locales. Install systemd or ensure localectl is in PATH.") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"localectl list-locales failed: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error fetching locales: {e}") from e

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