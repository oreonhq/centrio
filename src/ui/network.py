# centrio_installer/ui/network.py

import gi
import subprocess
import threading
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .base import BaseConfigurationPage


def _detect_connection_type():
    """Returns ('wired'|'wifi'|'none', connected: bool)."""
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,STATE,DEVICE", "dev", "status"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return "none", False
        wired_conn = False
        wifi_conn = False
        for line in r.stdout.strip().split("\n"):
            parts = line.split(":")
            if len(parts) >= 2:
                t, state = parts[0], (parts[1] if len(parts) > 1 else "").lower()
                connected = "connected" in state or "activated" in state
                if t == "ethernet" and connected:
                    wired_conn = True
                elif t == "wifi" and connected:
                    wifi_conn = True
        if wired_conn:
            return "wired", True
        if wifi_conn:
            return "wifi", True
        return "wifi" if subprocess.run(["nmcli", "-t", "-f", "NAME", "dev", "wifi"], capture_output=True, text=True, timeout=3).returncode == 0 else "none", False
    except Exception:
        return "none", False


def _get_wifi_networks():
    """Returns list of {"ssid": str, "signal": str, "security": str}."""
    out = []
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        seen = set()
        for line in r.stdout.strip().split("\n"):
            parts = line.split(":", 2)
            ssid = (parts[0] if len(parts) > 0 else "").strip() or "(hidden)"
            if ssid in seen:
                continue
            seen.add(ssid)
            signal = parts[1] if len(parts) > 1 else ""
            security = parts[2] if len(parts) > 2 else ""
            out.append({"ssid": ssid, "signal": signal, "security": security})
        return out[:20]
    except Exception:
        return []


class NetworkConnectivityPage(BaseConfigurationPage):
    """Page for network connectivity. Wired status, WiFi selection, or continue without network."""

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
        self.wifi_networks = []
        self._build_ui()
        self._check_network_status()

    def _build_ui(self):
        self.status_section = Adw.PreferencesGroup(title="Network Status", description="Current connectivity")
        self.add(self.status_section)
        self.status_row = Adw.ActionRow(title="Status", subtitle="Checking...")
        self.status_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self.status_row.add_prefix(self.status_icon)
        self.status_section.add(self.status_row)

        self.wifi_section = Adw.PreferencesGroup(title="Wi‑Fi Networks", description="Select a network to connect")
        self.wifi_section.set_visible(False)
        self.add(self.wifi_section)

        self.wifi_list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.wifi_list_box.add_css_class("boxed-list")
        self.wifi_section.add(self.wifi_list_box)

        self.rescan_row = Adw.ActionRow(title="Rescan", subtitle="Refresh the list of networks")
        self.rescan_btn = Gtk.Button(label="Rescan")
        self.rescan_btn.add_css_class("compact")
        self.rescan_btn.connect("clicked", self._rescan_wifi)
        self.rescan_row.add_suffix(self.rescan_btn)
        self.wifi_section.add(self.rescan_row)

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
            self.wifi_networks = _get_wifi_networks() if conn_type == "wifi" else []
            GLib.idle_add(self._update_ui)

        threading.Thread(target=check, daemon=True).start()

    def _update_ui(self):
        if self.network_status == "connected":
            if self.connection_type == "wired":
                self.status_row.set_subtitle("Already connected via wired network")
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
        self._populate_wifi()
        self.skip_btn.set_sensitive(True)

    def _populate_wifi(self):
        while True:
            child = self.wifi_list_box.get_row_at_index(0)
            if child is None:
                break
            self.wifi_list_box.remove(child)
        self.wifi_section.set_visible(self.connection_type == "wifi" and not self.network_enabled)
        if not self.wifi_section.get_visible():
            return
        for net in self.wifi_networks:
            row = Adw.ActionRow(
                title=net["ssid"],
                subtitle=f"Signal: {net['signal']}%  {net['security']}"
            )
            conn_btn = Gtk.Button(label="Connect")
            conn_btn.add_css_class("compact")
            conn_btn.connect("clicked", self._on_wifi_connect, net["ssid"])
            row.add_suffix(conn_btn)
            self.wifi_list_box.append(row)

    def _rescan_wifi(self, btn):
        btn.set_sensitive(False)
        def scan():
            self.wifi_networks = _get_wifi_networks()
            GLib.idle_add(lambda: (btn.set_sensitive(True), self._populate_wifi()))
        threading.Thread(target=scan, daemon=True).start()

    def _on_wifi_connect(self, btn, ssid):
        btn.set_sensitive(False)
        def connect():
            r = subprocess.run(
                ["nmcli", "dev", "wifi", "connect", ssid],
                capture_output=True, text=True, timeout=30
            )
            ok = r.returncode == 0
            GLib.idle_add(lambda: self._after_wifi_connect(btn, ok))

        threading.Thread(target=connect, daemon=True).start()

    def _after_wifi_connect(self, btn, ok):
        btn.set_sensitive(True)
        if ok:
            self.show_toast("Connected")
            self._check_network_status()
        else:
            self.show_toast("Connection failed. Check password and try again.")

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
