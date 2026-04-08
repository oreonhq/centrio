import subprocess

from PySide6.QtWidgets import QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage
from utils import ana_get_available_locales


class LanguagePage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="System Language",
            subtitle="Select the primary language for the installed system",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        locales = ana_get_available_locales()
        self.locale_codes = list(locales.keys())
        self.locale_combo = QComboBox()
        for code in self.locale_codes:
            self.locale_combo.addItem(f"{locales[code]} ({code})")
        self.page_layout.addWidget(QLabel("System Locale"))
        self.page_layout.addWidget(self.locale_combo)
        btn = QPushButton("Apply System Locale")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

    def apply_settings_and_return(self, _button=None):
        idx = self.locale_combo.currentIndex()
        if idx < 0 or idx >= len(self.locale_codes):
            self.show_toast("Invalid locale selection.")
            return
        selected_locale = self.locale_codes[idx]
        try:
            subprocess.run(["localectl", "set-locale", f"LANG={selected_locale}"], check=True, timeout=10)
        except Exception as e:
            self.show_toast(f"Error setting locale: {e}")
            return
        self.mark_complete_and_return(config_values={"locale": selected_locale})
