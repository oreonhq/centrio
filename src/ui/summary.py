from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

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

        subtitle = QLabel("Review and complete all required settings before proceeding.")
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

        self._add_config_row("keyboard",  "Keyboard Layout",          "Configure keyboard input method",             True)
        self._add_config_row("language",  "System Language",           "Set the default system locale",               False)
        self._add_config_row("timedate",  "Time & Date",               "Timezone and time synchronization",           True)
        self._add_config_row("network",   "Network Connectivity",      "Network for additional software",             True)
        self._add_config_row("disk",      "Installation Destination",  "Disk selection and partitioning method",      True)
        self._add_config_row("payload",   "Software Packages",         "Package selection and repositories",          True)
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
        self._update_installation_status()

    def _update_installation_status(self):
        required_keys = [k for k, c in self.config_rows.items() if c["required"]]
        done = sum(1 for k in required_keys if self.main_window.config_state.get(k, False))
        total = len(required_keys)
        if done == total:
            self.status_label.setText(f"All required settings configured. Ready to install.")
        else:
            self.status_label.setText(f"{total - done} required setting(s) remaining.")
