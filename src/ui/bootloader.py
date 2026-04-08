from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from .base import BaseConfigurationPage


class BootloaderPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Bootloader Configuration",
            subtitle="Confirm bootloader installation",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        self.enable_check = QCheckBox("Install Bootloader")
        self.enable_check.setChecked(True)
        self.page_layout.addWidget(self.enable_check)
        self.page_layout.addWidget(
            QLabel("Default location and settings are selected automatically.")
        )
        btn = QPushButton("Confirm Bootloader Choice")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)
        self.page_layout.addStretch(1)

    def apply_settings_and_return(self, _button=None):
        self.mark_complete_and_return(
            config_values={"install_bootloader": self.enable_check.isChecked()}
        )
