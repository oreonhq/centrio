import subprocess

from PySide6.QtWidgets import QComboBox, QLabel, QPushButton

from .base import BaseConfigurationPage
from utils import ana_get_keyboard_layouts


class KeyboardPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Keyboard Layout",
            subtitle="Select your keyboard layout",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        self.layout_pairs = ana_get_keyboard_layouts()
        self.layout_codes = [code for _, code in self.layout_pairs]
        self.layout_combo = QComboBox()
        for name, _ in self.layout_pairs:
            self.layout_combo.addItem(name)
        self.page_layout.addWidget(QLabel("Keyboard Layout"))
        self.page_layout.addWidget(self.layout_combo)
        btn = QPushButton("Apply Keyboard Layout")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

    def apply_settings_and_return(self, _button=None):
        idx = self.layout_combo.currentIndex()
        if idx < 0 or idx >= len(self.layout_codes):
            self.show_toast("Invalid keyboard layout selection.")
            return
        selected_layout = self.layout_codes[idx]
        try:
            subprocess.run(["localectl", "set-keymap", selected_layout], check=True, timeout=10)
        except Exception as e:
            self.show_toast(f"Error setting keyboard layout: {e}")
            return
        self.mark_complete_and_return(config_values={"layout": selected_layout})
