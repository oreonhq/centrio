# centrio_installer/utils.py


import os
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
    """Return full IANA timezone list. Prefer zoneinfo (Python 3.9+), then pytz, then minimal fallback."""
    try:
        from zoneinfo import available_timezones
        zones = sorted(available_timezones())
        if zones:
            print(f"  Loaded {len(zones)} timezones from zoneinfo.")
            return zones
    except ImportError:
        pass
    try:
        import pytz
        zones = sorted(pytz.all_timezones)
        print(f"  Loaded {len(zones)} timezones from pytz.")
        return zones
    except Exception as e:
        print(f"  Timezone fallback: {e}")
    return ["UTC", "GMT", "America/New_York", "Europe/London", "Asia/Tokyo", "Europe/Paris", "Asia/Kolkata", "Australia/Sydney"]


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


def ana_get_keyboard_layouts():
    """Fetches console keymaps and returns list of (display_name, keymap_code) for UI.
    Display names come from XKB evdev.lst where available; otherwise the code is shown.
    """
    print("Fetching keyboard layouts using localectl...")
    descriptions = _parse_xkb_layout_descriptions()
    try:
        result = subprocess.run(["localectl", "list-keymaps"],
                                capture_output=True, text=True, check=True, timeout=15)
        keymaps = sorted([line.strip() for line in result.stdout.split("\n") if line.strip()])
        if not keymaps:
            keymaps = ["us"]
        # Build (display_name, code) list; sort by display name
        pairs = []
        for code in keymaps:
            display = descriptions.get(code, code)
            pairs.append((display, code))
        pairs.sort(key=lambda x: x[0].lower())
        print(f"  Found {len(pairs)} keyboard layouts.")
        return pairs  # List of (display_name, keymap_code)
    except FileNotFoundError:
        print("ERROR: localectl command not found. Using fallback layouts.")
        return [("English (US)", "us"), ("English (UK)", "gb"), ("German", "de"), ("French", "fr")]
    except (subprocess.CalledProcessError, Exception) as e:
        print(f"ERROR: list-keymaps failed: {e}. Using fallback.")
        return [("English (US)", "us"), ("English (UK)", "gb"), ("German", "de"), ("French", "fr")]

def ana_get_available_locales():
    """Fetches available locales using localectl."""
    print("Fetching available locales using localectl...")
    locales = {}
    try:
        result = subprocess.run(["localectl", "list-locales"], 
                                capture_output=True, text=True, check=True)
        raw_locales = [line.strip() for line in result.stdout.split('\n') if line and '.' in line]
        # Attempt to generate a display name (basic)
        for locale_code in raw_locales:
             # Simple conversion for display: en_US.UTF-8 -> English (US) UTF-8
             parts = locale_code.split('.')[0].split('_')
             lang = parts[0]
             country = f"({parts[1]})" if len(parts) > 1 else ""
             # This name generation is very basic, ideally use a locale library
             display_name = f"{lang.capitalize()} {country}".strip()
             # Use code as key, display name as value (or vice-versa if needed by UI)
             locales[locale_code] = display_name 
             
        print(f"  Found {len(locales)} locales.")
        # Sort by display name for UI
        sorted_locales = dict(sorted(locales.items(), key=lambda item: item[1]))
        return sorted_locales if sorted_locales else {"en_US.UTF-8": "English (US)"} # Fallback
        
    except FileNotFoundError:
        print("ERROR: localectl command not found. Using fallback locales.")
    except subprocess.CalledProcessError as e:
        print(f"ERROR: localectl list-locales failed: {e}. Using fallback locales.")
    except Exception as e:
        print(f"ERROR: Unexpected error fetching locales: {e}. Using fallback locales.")
        
    # Fallback list if errors occurred
    return {
        "en_US.UTF-8": "English (US)",
        "es_ES.UTF-8": "Spanish (Spain)",
        "fr_FR.UTF-8": "French (France)",
        "de_DE.UTF-8": "German (Germany)"
    } 

# Note: Avoid importing GUI or app-specific constants here to keep utils lightweight.

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