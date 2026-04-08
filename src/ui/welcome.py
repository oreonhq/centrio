import os
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from utils import get_os_release_info


class WelcomePage(QWidget):
    def __init__(self, main_window=None, **kwargs):
        super().__init__(**kwargs)
        self.main_window = main_window
        self.language_codes = []
        self.selected_language = "en_US"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        os_info = get_os_release_info()
        distro_name = os_info.get("NAME", "Oreon")
        version = os_info.get("VERSION", "11").replace("10", "11")

        title = QLabel(f"Welcome to {distro_name}")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        desc = QLabel("Set up your new operating system in a few simple steps.")
        desc.setWordWrap(True)
        desc.setObjectName("pageSubtitle")
        layout.addWidget(desc)

        lang_card = QFrame()
        lang_card.setObjectName("card")
        lang_layout = QVBoxLayout(lang_card)
        lang_layout.setContentsMargins(12, 12, 12, 12)
        lang_layout.addWidget(QLabel("Installer Language"))
        self.lang_combo = QComboBox()
        languages = [
            ("English (US)", "en_US"),
            ("English (UK)", "en_GB"),
            ("Español", "es_ES"),
            ("Français", "fr_FR"),
            ("Deutsch", "de_DE"),
            ("Italiano", "it_IT"),
            ("Português (Brasil)", "pt_BR"),
        ]
        for name, code in languages:
            self.lang_combo.addItem(name)
            self.language_codes.append(code)
        lang_layout.addWidget(self.lang_combo)
        layout.addWidget(lang_card)

        current_lang = self._detect_current_language() or "en_US"
        if current_lang in self.language_codes:
            self.lang_combo.setCurrentIndex(self.language_codes.index(current_lang))
        self.lang_combo.currentIndexChanged.connect(self.on_language_changed)

        info_card = QFrame()
        info_card.setObjectName("card")
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(12, 12, 12, 12)
        info_layout.addWidget(QLabel(f"Operating System: {distro_name} {version}"))
        layout.addWidget(info_card)
        layout.addSpacing(6)

    def on_language_changed(self, index):
        if index < 0 or index >= len(self.language_codes):
            return
        lang_code = self.language_codes[index]
        self.selected_language = lang_code
        lang_file = getattr(self.main_window, "installer_lang_file", None)
        script = getattr(self.main_window, "installer_script", None)
        if not lang_file or not script:
            return
        try:
            locale_value = f"{lang_code}.UTF-8" if "." not in lang_code else lang_code
            with open(lang_file, "w", encoding="utf-8") as f:
                f.write(locale_value)
        except Exception as e:
            print(f"Could not write installer language file: {e}")
            return

        reply = QMessageBox.information(
            self,
            "Language Selected",
            "The installer will restart to apply the new language.",
            QMessageBox.StandardButton.Ok,
        )
        if reply == QMessageBox.StandardButton.Ok:
            try:
                os.execv(sys.executable, [sys.executable, script] + sys.argv[1:])
            except Exception as e:
                print(f"Could not restart installer: {e}")

    def _detect_current_language(self):
        lang = os.environ.get("LANG", "")
        if lang:
            return lang.split(".")[0]
        return None
