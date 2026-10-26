from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import accounts

ERROR_STYLE = "color: #e01b24; font-size: 9pt;"

ICONS = {True: "✓", "required": "!", "optional": "○"}


class SummaryPage(QWidget):
    def __init__(self, main_window, **kwargs):
        super().__init__(**kwargs)
        self.main_window = main_window
        self.config_rows = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 16)
        outer.setSpacing(14)

        title = QLabel("Installation Summary")
        title.setObjectName("pageTitle")
        outer.addWidget(title)

        subtitle = QLabel("Review and complete installation settings before proceeding.")
        subtitle.setObjectName("pageSubtitle")
        outer.addWidget(subtitle)

        # scrollable rows area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        rows_container = QWidget()
        rows_container.setObjectName("scrollContent")
        self.rows_layout = QVBoxLayout(rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)
        scroll.setWidget(rows_container)
        outer.addWidget(scroll, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("pageSubtitle")
        outer.addWidget(self.status_label)

        self._add_config_row("network",   "Network Connectivity",      "Network for additional software",             True)
        self._add_config_row("disk",      "Installation Destination",  "Disk selection and partitioning method",      True)
        self._add_config_row("payload",   "Software Packages",         "Package selection and repositories",          True)
        self._build_account_card()

    def _build_account_card(self):
        self.account_touched = set()
        card = QFrame()
        card.setObjectName("card")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        title = QLabel("Server Accounts")
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        hint = QLabel("Server installs have no setup wizard. Create a user, enable root, or both.")
        hint.setObjectName("rowSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.user_check = QCheckBox("Create a user account")
        self.user_check.setChecked(True)
        layout.addWidget(self.user_check)
        self.user_box = QWidget()
        user_layout = QVBoxLayout(self.user_box)
        user_layout.setContentsMargins(20, 0, 0, 0)
        user_layout.setSpacing(6)
        self.full_name_edit = self._line_edit("Full name", user_layout)
        self.username_edit = self._line_edit("Username", user_layout)
        self.username_error = self._error_label(user_layout)
        self.user_password_edit = self._line_edit("Password", user_layout, True)
        self.user_confirm_edit = self._line_edit("Confirm password", user_layout, True)
        self.user_password_error = self._error_label(user_layout)
        self.admin_check = QCheckBox("Administrator (wheel group)")
        self.admin_check.setChecked(True)
        user_layout.addWidget(self.admin_check)
        layout.addWidget(self.user_box)

        self.root_check = QCheckBox("Enable the root account")
        layout.addWidget(self.root_check)
        self.root_box = QWidget()
        root_layout = QVBoxLayout(self.root_box)
        root_layout.setContentsMargins(20, 0, 0, 0)
        root_layout.setSpacing(6)
        self.root_password_edit = self._line_edit("Root password", root_layout, True)
        self.root_confirm_edit = self._line_edit("Confirm root password", root_layout, True)
        self.root_password_error = self._error_label(root_layout)
        layout.addWidget(self.root_box)
        self.general_error = self._error_label(layout)

        for edit, name in (
            (self.full_name_edit, "full_name"),
            (self.username_edit, "username"),
            (self.user_password_edit, "user_password"),
            (self.user_confirm_edit, "user_password"),
            (self.root_password_edit, "root_password"),
            (self.root_confirm_edit, "root_password"),
        ):
            edit.textChanged.connect(lambda _t, n=name: self._on_account_field_changed(n))
        self.user_check.toggled.connect(lambda _c: self._on_account_changed())
        self.root_check.toggled.connect(lambda _c: self._on_account_changed())
        self.admin_check.toggled.connect(lambda _c: self.main_window.update_navigation())

        self.account_card = card
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, card)
        self._refresh_account_card()

    def _line_edit(self, placeholder, layout, password=False):
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(edit)
        return edit

    def _error_label(self, layout):
        label = QLabel("")
        label.setStyleSheet(ERROR_STYLE)
        label.setWordWrap(True)
        label.setVisible(False)
        layout.addWidget(label)
        return label

    def is_server_install(self):
        payload = self.main_window.final_config.get("payload") or {}
        return bool(payload.get("server_install", False))

    def _account_errors(self):
        return accounts.validate_accounts(
            self.user_check.isChecked(),
            self.username_edit.text(),
            self.user_password_edit.text(),
            self.user_confirm_edit.text(),
            self.root_check.isChecked(),
            self.root_password_edit.text(),
            self.root_confirm_edit.text(),
        )

    def accounts_ready(self):
        if not self.is_server_install():
            return True
        return not self._account_errors()

    def build_accounts_config(self):
        return accounts.build_accounts_config(
            self.user_check.isChecked(),
            self.full_name_edit.text().strip(),
            self.username_edit.text(),
            self.user_password_edit.text(),
            self.admin_check.isChecked(),
            self.root_check.isChecked(),
            self.root_password_edit.text(),
        )

    def _on_account_field_changed(self, name):
        self.account_touched.add(name)
        self._on_account_changed()

    def _on_account_changed(self):
        self._refresh_account_card()
        self.main_window.update_navigation()

    def _refresh_account_card(self):
        server = self.is_server_install()
        self.account_card.setVisible(server)
        user_on = self.user_check.isChecked()
        root_on = self.root_check.isChecked()
        self.user_box.setVisible(user_on)
        self.root_box.setVisible(root_on)
        errors = self._account_errors() if server else {}
        self._show_error(self.username_error, errors.get("username"), user_on and "username" in self.account_touched)
        self._show_error(self.user_password_error, errors.get("user_password"), user_on and "user_password" in self.account_touched)
        self._show_error(self.root_password_error, errors.get("root_password"), root_on and "root_password" in self.account_touched)
        self._show_error(self.general_error, errors.get("general"), True)
        if hasattr(self, "status_label"):
            self._update_installation_status()

    def _show_error(self, label, message, allowed):
        label.setText(message or "")
        label.setVisible(bool(message) and allowed)

    def _add_config_row(self, key, title, subtitle_base, required):
        card = QFrame()
        card.setObjectName("card")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setFixedHeight(70)

        row = QHBoxLayout(card)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(14)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 600;")

        status_label = QLabel()
        status_label.setObjectName("rowSubtitle")

        text_col.addWidget(title_label)
        text_col.addWidget(status_label)
        row.addLayout(text_col, 1)

        btn = QPushButton("Configure")
        btn.setObjectName("primaryButton")
        btn.setFixedWidth(110)
        btn.setFixedHeight(36)
        btn.clicked.connect(lambda _=False, k=key: self.on_row_activated(k))
        row.addWidget(btn)

        # insert before the trailing stretch
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, card)

        if key not in self.main_window.config_state:
            self.main_window.config_state[key] = False

        self.config_rows[key] = {
            "title_label":  title_label,
            "status_label": status_label,
            "required":     required,
            "subtitle_base": subtitle_base,
            "title":        title,
            "card":         card,
        }
        self.update_row_status(key, self.main_window.config_state.get(key, False))

    def on_row_activated(self, key):
        self.main_window.navigate_to_config(key)

    def update_row_status(self, key, is_complete):
        if key not in self.config_rows:
            return
        cfg = self.config_rows[key]
        if is_complete:
            cfg["status_label"].setText("Configured")
            cfg["status_label"].setStyleSheet("font-size: 10pt;")
        elif cfg["required"]:
            cfg["status_label"].setText("Required")
            cfg["status_label"].setStyleSheet("font-size: 10pt;")
        else:
            cfg["status_label"].setText("Optional")
            cfg["status_label"].setStyleSheet("font-size: 10pt;")
        if hasattr(self, "account_card"):
            self._refresh_account_card()
        self._update_installation_status()

    def _update_installation_status(self):
        required_keys = [k for k, c in self.config_rows.items() if c["required"]]
        done = sum(1 for k in required_keys if self.main_window.config_state.get(k, False))
        total = len(required_keys)
        if done == total and not self.accounts_ready():
            self.status_label.setText("Set up a server user account or a root password.")
        elif done == total:
            self.status_label.setText(f"All required settings configured. Ready to install.")
        else:
            self.status_label.setText(f"{total - done} required setting(s) remaining.")
