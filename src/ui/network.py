import subprocess

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout

from .base import BaseConfigurationPage


def _detect_connection_type():
    try:
        r_conn = subprocess.run(
            ["nmcli", "networking", "connectivity", "check"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        connected = r_conn.returncode == 0 and (r_conn.stdout or "").strip().lower() in ("full", "limited")
        r_active = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        conn_type = "none"
        if r_active.returncode == 0:
            for line in (r_active.stdout or "").splitlines():
                t = (line.split(":", 1)[0] or "").lower()
                if t in ("802-3-ethernet", "ethernet"):
                    conn_type = "wired"
                    break
                if t in ("wifi", "802-11-wireless", "wireless"):
                    conn_type = "wifi"
                    break
        return conn_type, connected
    except Exception:
        return "none", False


class NetworkConnectivityPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Network Connectivity",
            subtitle="Connect to a network for additional software, or continue without network",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        self.network_status = "unknown"
        self.connection_type = "none"
        self.network_enabled = False

        self.status_label = QLabel("Checking...")
        self.page_layout.addWidget(self.status_label)

        self.apply_btn = QPushButton("Use network for additional software")
        self.apply_btn.clicked.connect(self._on_apply)
        self.page_layout.addWidget(self.apply_btn)

        self.skip_btn = QPushButton("Continue without network")
        self.skip_btn.clicked.connect(self._on_skip)
        self.page_layout.addWidget(self.skip_btn)

        self.page_layout.addStretch(1)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_status)
        self.refresh_timer.start(3000)
        self.refresh_status()

    def refresh_status(self):
        conn_type, connected = _detect_connection_type()
        self.connection_type = conn_type
        self.network_status = "connected" if connected else "disconnected"
        self.network_enabled = connected
        if connected:
            self.status_label.setText(f"Connected via {conn_type}.")
            self.apply_btn.setEnabled(True)
        else:
            self.status_label.setText("No network connection.")
            self.apply_btn.setEnabled(False)

    def _on_apply(self):
        config = {
            "network_enabled": self.network_enabled,
            "skip_network": False,
            "network_status": self.network_status,
        }
        self.mark_complete_and_return(config_values=config)

    def _on_skip(self):
        config = {
            "network_enabled": False,
            "skip_network": True,
            "network_status": self.network_status,
        }
        self.mark_complete_and_return(config_values=config)

    def _get_page_key(self):
        return "network"
