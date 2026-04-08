from PySide6.QtWidgets import QCheckBox, QFormLayout, QLineEdit, QPushButton, QWidget

from .base import BaseConfigurationPage


class UserPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="User Creation",
            subtitle="Create an initial user account",
            main_window=main_window,
            overlay_widget=overlay_widget,
            **kwargs,
        )
        form_host = QWidget()
        form = QFormLayout(form_host)
        self.real_name = QLineEdit()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.admin = QCheckBox("Make this user an administrator")
        self.admin.setChecked(True)
        form.addRow("Full Name", self.real_name)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("Confirm Password", self.confirm)
        form.addRow("", self.admin)
        self.page_layout.addWidget(form_host)
        btn = QPushButton("Create User Account")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)

    def apply_settings_and_return(self, _button=None):
        username = self.username.text().strip()
        password = self.password.text()
        if not username or password != self.confirm.text():
            self.show_toast("Please ensure username is valid and passwords match.")
            return
        config_values = {
            "username": username,
            "real_name": self.real_name.text().strip(),
            "password": password,
            "is_admin": self.admin.isChecked(),
        }
        self.mark_complete_and_return(config_values=config_values)
