import subprocess

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage
from utils import ana_get_all_regions_and_timezones


class TimeDatePage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Time & Date",
            subtitle="Set timezone and time settings",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        self.timezones = ana_get_all_regions_and_timezones()
        self.tz_combo = QComboBox()
        self.tz_combo.addItems(self.timezones[:])
        self.ntp_check = QCheckBox("Enable Network Time Protocol (NTP)")
        self.ntp_check.setChecked(True)
        self.page_layout.addWidget(QLabel("Timezone"))
        self.page_layout.addWidget(self.tz_combo)
        self.page_layout.addWidget(self.ntp_check)
        btn = QPushButton("Apply Time & Date Settings")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

    def apply_settings_and_return(self, _button=None):
        idx = self.tz_combo.currentIndex()
        if idx < 0:
            self.show_toast("Invalid timezone selection.")
            return
        selected_tz = self.timezones[idx]
        ntp = self.ntp_check.isChecked()
        try:
            subprocess.run(["timedatectl", "set-timezone", selected_tz], check=True, timeout=8)
            subprocess.run(["timedatectl", "set-ntp", "true" if ntp else "false"], check=True, timeout=8)
        except Exception as e:
            self.show_toast(f"Error applying time settings: {e}")
            return
        self.mark_complete_and_return(config_values={"timezone": selected_tz, "ntp": ntp})
