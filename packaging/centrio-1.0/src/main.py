#!/usr/bin/env python3
"""
Centrio Installer - Main entry point
"""

import sys
import os
import platform

# work around MESA "Failed to attach to x11 shm" (common on ARM/Wayland)
if platform.machine().lower() in ("aarch64", "arm64"):
    for k, v in [
        ("GDK_BACKEND", "x11"),
        ("LIBGL_ALWAYS_SOFTWARE", "1"),
        ("GALLIUM_DRIVER", "llvmpipe"),
        # Disable MIT-SHM so Mesa doesn't try to attach to X11 shared memory
        ("QT_X11_NO_MITSHM", "1"),
        ("_X11_NO_MITSHM", "1"),
        ("_MITSHM", "0"),
    ]:
        os.environ[k] = v
import subprocess
import logging
import gettext
import locale
from pathlib import Path

# --- Privileged helper mode (for live session: oreon-installer-priv runs us as root) ---
if "--backend=priv" in sys.argv:
    idx = sys.argv.index("--backend=priv")
    cmd_argv = sys.argv[idx + 1:]
    if not cmd_argv:
        sys.exit(1)
    try:
        result = subprocess.run(cmd_argv, stdin=sys.stdin, timeout=None)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"Command not found: {cmd_argv[0]}", file=sys.stderr)
        sys.exit(127)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)

# File where chosen installer language is stored (restart picks it up)
INSTALLER_LANG_FILE = os.environ.get(
    "CENTRIO_INSTALLER_LANG_FILE",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "centrio_installer_lang")
)


def setup_i18n():
    """Set up internationalization from installer language file or system locale."""
    # When installed: /usr/share/centrio/main.py -> parent = /usr/share/centrio
    # When development: src/main.py -> parent = src, parent.parent = repo root
    _dir = Path(__file__).resolve().parent
    project_root = _dir.parent if (_dir / "locale").exists() else _dir
    locale_dir = str(project_root / "locale")

    current_locale = None
    if os.path.isfile(INSTALLER_LANG_FILE):
        try:
            with open(INSTALLER_LANG_FILE, "r", encoding="utf-8") as f:
                current_locale = f.read().strip()
            if current_locale and ".UTF-8" not in current_locale and "." not in current_locale:
                current_locale = f"{current_locale}.UTF-8"
        except Exception as e:
            print(f"Warning: Could not read installer lang file: {e}")

    if not current_locale:
        current_locale = locale.getlocale()[0]
    if not current_locale:
        current_locale = os.environ.get("LANG", "en_US") or "en_US"
    if current_locale and ".UTF-8" not in current_locale and "." not in current_locale:
        current_locale = f"{current_locale}.UTF-8"

    try:
        os.environ["LANG"] = current_locale
        os.environ["LC_ALL"] = current_locale
        locale.setlocale(locale.LC_ALL, current_locale)
    except locale.Error:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass

    gettext.install("centrio", localedir=locale_dir)
    print(f"Internationalization set up for locale: {current_locale}")
    return current_locale


def main():
    """Main entry point for the Centrio installer."""
    setup_i18n()

    # Optional: --frontend=gis (run in GNOME Initial Setup / live kiosk; no extra handling needed)
    if "--frontend=gis" in sys.argv:
        pass  # Run GUI as usual; GIS/kiosk is environment

    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw
    from window import CentrioInstallerWindow

    app = Adw.Application(
        application_id="org.centrio.installer",
        flags=0
    )
    installer_script = os.path.abspath(sys.argv[0])

    def on_activate(app):
        logging.info("Centrio Installer starting...")
        win = CentrioInstallerWindow(application=app, installer_script=installer_script)
        win.installer_lang_file = INSTALLER_LANG_FILE
        win.present()

    app.connect("activate", on_activate)
    exit_status = app.run(sys.argv)
    return exit_status

if __name__ == "__main__":
    sys.exit(main()) 