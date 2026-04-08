# Centrio Installer
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# centrio_installer/ui/base.py

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class BaseConfigurationPage(QWidget):
    """Base class for configuration pages with common behavior."""

    def __init__(self, title, subtitle="", main_window=None, overlay_widget=None, use_card=True, **kwargs):
        super().__init__(**kwargs)
        self.main_window = main_window
        self.overlay_widget = overlay_widget
        self._toast_label = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 16)
        outer.setSpacing(10)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        outer.addWidget(title_label)

        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setWordWrap(True)
            subtitle_label.setObjectName("pageSubtitle")
            outer.addWidget(subtitle_label)

        outer.addSpacing(6)

        if use_card:
            card = QFrame()
            card.setObjectName("card")
            card.setFrameShape(QFrame.Shape.StyledPanel)
            self.page_layout = QVBoxLayout(card)
            self.page_layout.setContentsMargins(16, 14, 16, 14)
            self.page_layout.setSpacing(10)
            outer.addWidget(card, 1)
        else:
            # no card wrapper - page manages its own layout structure
            self.page_layout = outer

    def show_toast(self, message, timeout=3):
        if self.main_window and hasattr(self.main_window, "show_status_message"):
            self.main_window.show_status_message(message, timeout * 1000)
            return
        print(f"Toast: {message}")

    def mark_complete_and_return(self, _button=None, config_values=None):
        if not self.main_window:
            print("Warning: No main_window reference available for marking completion.")
            return
        page_key = self._get_page_key()
        if not page_key:
            print("Warning: Could not determine page key for completion marking.")
            return
        self.main_window.mark_config_complete(page_key, True, config_values)
        QTimer.singleShot(0, self.main_window.return_to_summary)

    def _get_page_key(self):
        class_name = self.__class__.__name__
        if class_name.endswith("Page"):
            return class_name[:-4].lower()
        return None

    def connect_and_fetch_data(self):
        pass

    def apply_settings_and_return(self, _button=None):
        pass