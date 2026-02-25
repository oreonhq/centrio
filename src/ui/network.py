# centrio_installer/ui/network.py

import gi
import subprocess
import threading
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .base import BaseConfigurationPage


def _detect_connection_type():
    """Returns ('wired'|'wifi'|'none', connected: bool). Uses nmcli networking connectivity
    for reliable connectivity; dev status alone can report 'connected' incorrectly."""
    try:
        r_conn = subprocess.run(
            ["nmcli", "networking", "connectivity", "check"],
            capture_output=True, text=True, timeout=5
        )
        actually_connected = False
        if r_conn.returncode == 0 and r_conn.stdout:
            out = r_conn.stdout.strip().lower()
            actually_connected = out in ("full", "limited")

        conn_type = "none"
        r_active = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5
        )
        if r_active.returncode == 0 and r_active.stdout.strip():
            for line in r_active.stdout.strip().split("\n"):
                parts = line.split(":", 1)
                t = (parts[0] or "").lower()
                if t == "loopback":
                    continue
                if t in ("802-3-ethernet", "ethernet"):
                    conn_type = "wired"
                    break
                if t in ("wifi", "802-11-wireless", "wireless"):
                    conn_type = "wifi"
                    break

        if conn_type == "none":
            r_wifi = subprocess.run(
                ["nmcli", "-t", "-f", "TYPE", "dev", "status"],
                capture_output=True, text=True, timeout=3
            )
            if r_wifi.returncode == 0 and "wifi" in (r_wifi.stdout or "").lower():
                conn_type = "wifi"

        return conn_type, actually_connected
    except Exception:
        return "none", False


class NetworkConnectivityPage(BaseConfigurationPage):
    """Page for network connectivity. Status and instructions to use control center for Wi‑Fi."""

    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Network Connectivity",
            subtitle="Connect to a network for additional software, or continue without network",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs
        )
        self.network_enabled = False
        self.skip_network = False
        self.network_status = "unknown"
        self.connection_type = "none"
        self._build_ui()
        self._check_network_status()

    def _build_ui(self):
        self.status_section = Adw.PreferencesGroup(title="Network Status", description="Current connectivity")
        self.add(self.status_section)
        self.status_row = Adw.ActionRow(title="Status", subtitle="Checking...")
        self.status_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self.status_row.add_prefix(self.status_icon)
        self.status_section.add(self.status_row)

        self.help_section = Adw.PreferencesGroup(
            title="Connecting to Wi‑Fi",
            description="To connect to Wi‑Fi, use the control center in the bottom panel below this installer."
        )
        self.add(self.help_section)

        self.buttons_section = Adw.PreferencesGroup(title="Continue", description="")
        self.buttons_section.set_margin_top(6)
        self.add(self.buttons_section)
        self.apply_row = Adw.ActionRow(
            title="Use network for additional software",
            subtitle="Proceed with network connection"
        )
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.add_css_class("compact")
        self.apply_btn.connect("clicked", self._on_apply)
        self.apply_row.add_suffix(self.apply_btn)
        self.buttons_section.add(self.apply_row)

        self.skip_row = Adw.ActionRow(
            title="Continue without network",
            subtitle="Install only the base system"
        )
        self.skip_btn = Gtk.Button(label="Continue without network")
        self.skip_btn.add_css_class("compact")
        self.skip_btn.connect("clicked", self._on_skip)
        self.skip_row.add_suffix(self.skip_btn)
        self.buttons_section.add(self.skip_row)

    def _check_network_status(self):
        def check():
            conn_type, connected = _detect_connection_type()
            self.connection_type = conn_type
            self.network_status = "connected" if connected else "disconnected"
            self.network_enabled = connected
            GLib.idle_add(self._update_ui)

        threading.Thread(target=check, daemon=True).start()

    def _update_ui(self):
        if self.network_status == "connected":
            if self.connection_type == "wired":
                self.status_row.set_subtitle("Connected via wired network")
                self.status_icon.set_from_icon_name("network-wired-symbolic")
            else:
                self.status_row.set_subtitle("Connected via Wi‑Fi")
                self.status_icon.set_from_icon_name("network-wireless-symbolic")
            self.status_icon.add_css_class("success")
            self.apply_btn.set_sensitive(True)
        else:
            self.status_row.set_subtitle("No network connection")
            self.status_icon.set_from_icon_name("network-offline-symbolic")
            self.status_icon.add_css_class("error")
            self.apply_btn.set_sensitive(False)
        self.skip_btn.set_sensitive(True)

    def _on_apply(self, btn):
        self.skip_network = False
        self.network_enabled = self.network_status == "connected"
        config = {
            "network_enabled": self.network_enabled,
            "skip_network": False,
            "network_status": self.network_status,
        }
        self.show_toast("Network settings applied.")
        super().mark_complete_and_return(btn, config_values=config)

    def _on_skip(self, btn):
        self.skip_network = True
        self.network_enabled = False
        config = {
            "network_enabled": False,
            "skip_network": True,
            "network_status": self.network_status,
        }
        self.show_toast("Continuing without network. Only base system will be installed.")
        super().mark_complete_and_return(btn, config_values=config)

    def _get_page_key(self):
        return "network"
