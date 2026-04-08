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
# centrio_installer/window.py
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

# Import Page classes (Fixed to absolute imports)
from ui.welcome import WelcomePage
from ui.summary import SummaryPage
from ui.progress import ProgressPage
from ui.finished import FinishedPage
from ui.keyboard import KeyboardPage
from ui.language import LanguagePage
from ui.timedate import TimeDatePage
from ui.disk import DiskPage
from ui.network import NetworkConnectivityPage
from ui.payload import PayloadPage
class CentrioInstallerWindow(QMainWindow):
    def __init__(self, installer_script=None, **kwargs):
        super().__init__(**kwargs)
        self.installer_script = installer_script or sys.argv[0]

        self.config_state = {} # Stores completion status (True/False) for each config key
        self.required_configs = set() # Set of keys for required configurations
        self.main_page_order = ["welcome", "summary", "progress", "finished"]
        # All known configuration page keys
        self.config_page_keys = ["keyboard", "language", "timedate", "disk", "network", "payload"]
        self.final_config = {} # Stores final selected values passed back from ui

        self.setWindowTitle("Centrio Installer")
        self.resize(900, 560)
        self.setMinimumSize(820, 520)
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        main_box = QVBoxLayout(central)
        main_box.setContentsMargins(0, 0, 0, 0)
        main_box.setSpacing(0)
        self.view_stack = QStackedWidget()
        main_box.addWidget(self.view_stack, 1)

        # --- Add ui to the stack ---
        # Main flow ui (pass main_window so welcome can restart for language change)
        self.welcome_page = WelcomePage(main_window=self)
        self.view_stack.addWidget(self.welcome_page)

        # Create Summary Page - this will also populate config_state and required_configs
        self.summary_page = SummaryPage(main_window=self)
        self.view_stack.addWidget(self.summary_page)

        self.progress_page = ProgressPage()
        self.view_stack.addWidget(self.progress_page)

        self.finished_page = FinishedPage(app=None)
        self.view_stack.addWidget(self.finished_page)

        # Configuration ui - Pass main_window and the overlay
        self.keyboard_page = KeyboardPage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.keyboard_page)
        
        self.language_page = LanguagePage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.language_page)
        
        self.timedate_page = TimeDatePage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.timedate_page)
        
        self.disk_page = DiskPage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.disk_page)
        
        self.network_page = NetworkConnectivityPage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.network_page)
        
        self.payload_page = PayloadPage(main_window=self, overlay_widget=None)
        self.view_stack.addWidget(self.payload_page)

        self.page_widgets = {
            "welcome": self.welcome_page,
            "summary": self.summary_page,
            "progress": self.progress_page,
            "finished": self.finished_page,
            "keyboard": self.keyboard_page,
            "language": self.language_page,
            "timedate": self.timedate_page,
            "disk": self.disk_page,
            "network": self.network_page,
            "payload": self.payload_page,
        }
        
        # Ensure required_configs is populated based on SummaryPage rows
        # (Should be done within SummaryPage._add_config_row now)
        for key, config in self.summary_page.config_rows.items():
            if config["required"]:
                self.required_configs.add(key)
            # Ensure config_state has an entry for every row added
            if key not in self.config_state:
                self.config_state[key] = False 

        # --- Navigation bar ---
        nav_widget = QWidget()
        nav_widget.setObjectName("navBar")
        nav_box = QHBoxLayout(nav_widget)
        nav_box.setContentsMargins(16, 10, 16, 10)
        nav_box.addStretch()
        main_box.addWidget(nav_widget)

        self.abort_button = QPushButton("Abort")
        self.abort_button.setObjectName("dangerButton")
        self.abort_button.clicked.connect(self.exit_window)
        nav_box.addWidget(self.abort_button)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.go_back)
        nav_box.addWidget(self.back_button)

        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("primaryButton")
        self.next_button.clicked.connect(self.go_next)
        nav_box.addWidget(self.next_button)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        # Set initial navigation state
        self.navigate_to_page("welcome")
        self.update_navigation()

    def show_status_message(self, message, timeout_ms=3000):
        self.status_bar.showMessage(message, timeout_ms)

    def _on_visible_child_changed(self, *args):
        self.update_navigation()
        name = self.get_current_page_info()[0]
        if name == "network" and hasattr(self, "network_page"):
            self.network_page.refresh_status()
        if name == "disk" and hasattr(self, "disk_page"):
            self.disk_page.refresh_for_network()
        if name == "payload" and hasattr(self, "payload_page"):
            self.payload_page.refresh_for_network(self.final_config.get("network", {}))

    def get_current_page_info(self):
        """Helper to get information about the currently visible page."""
        current_widget = self.view_stack.currentWidget()
        current_page_name = None
        for key, widget in self.page_widgets.items():
            if widget is current_widget:
                current_page_name = key
                break
        is_main_page = current_page_name in self.main_page_order
        is_config_page = current_page_name in self.config_page_keys
        try:
            main_index = self.main_page_order.index(current_page_name) if is_main_page else -1
        except ValueError:
            main_index = -1
        return current_page_name, is_main_page, is_config_page, main_index

    def navigate_to_page(self, page_name):
        """Sets the visible page in the view stack."""
        # Ensure this runs in the main GTK thread
        page = self.page_widgets.get(page_name)
        if page:
            self.view_stack.setCurrentWidget(page)
            self._on_visible_child_changed()

    def navigate_to_config(self, config_key):
        """Navigates to a specific configuration page by its key."""
        if config_key in self.config_page_keys:
            self.navigate_to_page(config_key)

    def return_to_summary(self):
        """Navigates back to the summary page."""
        self.navigate_to_page("summary")

    def mark_config_complete(self, key, is_complete, config_values=None):
        """Mark config as complete, store/update final values, and update summary UI."""
        if key in self.config_state:
            print(f">>> mark_config_complete called for key='{key}', is_complete={is_complete}, has_config_values={config_values is not None}")
            self.config_state[key] = is_complete

            # Always store the latest values when page marks itself complete.
            # Users often revisit network/payload and stale values break installs.
            if is_complete and config_values is not None:
                 self.final_config[key] = config_values
                 print(f"  Stored/updated final config for '{key}': {config_values}")
            elif not is_complete and key in self.final_config:
                 # Remove config if marked incomplete again (e.g., user goes back)
                 print(f"  Removing final config for '{key}'")
                 del self.final_config[key]
            elif is_complete and config_values is None:
                 print(f"  Skipping config storage for '{key}' (no config_values provided).")
                 
            # Update the corresponding row in the SummaryPage
            if hasattr(self, 'summary_page'): # Ensure summary page exists
                self.summary_page.update_row_status(key, is_complete)
                
            # Re-evaluate navigation state if we are on the summary page
            current_page_name, _, _, _ = self.get_current_page_info()
            if current_page_name == "summary":
                self.update_navigation()
        else:
             print(f"Warning: Attempted to mark completion for unknown key: {key}")

    def go_next(self, button=None):
        """Handles the action for the Next/Confirm/Begin button."""
        current_page_name, is_main_page, is_config_page, main_index = self.get_current_page_info()

        if is_config_page:
            # If on a config page, the button action is typically handled
            # by the page itself (apply_settings_and_return). 
            # This might be redundant, but ensures we return to summary.
            print(f"'Next' clicked on config page '{current_page_name}', returning to summary.")
            self.return_to_summary()
        elif is_main_page:
            if current_page_name == "summary":
                # --- Begin Installation --- 
                print("Configuration complete, starting installation progress...")
                # Navigate first, then start installation with collected data
                self.navigate_to_page("progress")
                self.progress_page.start_installation(self, self.final_config)
            elif main_index < len(self.main_page_order) - 1:
                # --- Navigate to Next Main Page --- 
                next_page_name = self.main_page_order[main_index + 1]
                print(f"Navigating from '{current_page_name}' to '{next_page_name}'")
                self.navigate_to_page(next_page_name)
            elif current_page_name == "finished":
                 print("'Next' clicked on finished page - Quitting.")
                 self.close()
            else:
                 print(f"Warning: 'Next' clicked on unexpected main page: {current_page_name}")

    def go_back(self, button=None):
        """Handles the action for the Back/Cancel button."""
        current_page_name, is_main_page, is_config_page, main_index = self.get_current_page_info()

        if is_config_page:
            # If on a config page, 'Back' means cancel and return to summary
            print(f"'Back' (Cancel) clicked on config page '{current_page_name}', returning to summary.")
            # Optionally, mark the config as incomplete again?
            # self.mark_config_complete(current_page_name, False)
            self.return_to_summary()
        elif is_main_page and main_index > 0:
            # --- Navigate to Previous Main Page --- 
            prev_page_name = self.main_page_order[main_index - 1]
            print(f"Navigating back from '{current_page_name}' to '{prev_page_name}'")
            if current_page_name == "progress": # Stop installation if going back from progress
                 self.progress_page.stop_installation()
            self.navigate_to_page(prev_page_name)
        else:
             print(f"Warning: 'Back' clicked on first page ('{current_page_name}') or unknown page.")

    def exit_window(self, button=None):
        """Handles the action for the Abort/Exit button."""
        print("Installation aborted by user.")
        print("Exiting Centrio Installer...")
        self.close()

    def update_navigation(self, stack=None, param=None):
        """Update the state of back/next buttons based on the current page."""
        # Use idle_add to prevent issues if called during stack transitions
        self._update_navigation_idle()

    def _update_navigation_idle(self):
        """Actual navigation update logic."""
        current_page_name, is_main_page, is_config_page, main_index = self.get_current_page_info()

        if not current_page_name:
             # Should not happen, but handle defensively
             self.back_button.setEnabled(False)
             self.next_button.setEnabled(False)
             return

        # --- Back Button Logic --- 
        if is_config_page:
            self.back_button.setEnabled(True)
            self.back_button.setText("Cancel")
            self.back_button.setVisible(True)
        elif is_main_page:
            self.back_button.setText("Back")
            # Can go back if not on welcome or progress page
            can_go_back = main_index > 0 and current_page_name != "progress" and current_page_name != "finished"
            self.back_button.setEnabled(can_go_back)
            self.back_button.setVisible(current_page_name != "finished")
        else:
            # Should be unreachable if ui are named correctly
            self.back_button.setEnabled(False)
            self.back_button.setVisible(True)

        # --- Next Button Logic --- 
        self.next_button.setVisible(True)
        
        if is_config_page:
            # Config ui handle their own primary action via their own buttons.
            # The main 'Next' button should ideally just return to summary.
            self.next_button.setText("Return to Summary")
            self.next_button.setEnabled(True)
            # We could hide this button and rely only on the page's button + Cancel?
            # self.next_button.set_visible(False) 
        elif current_page_name == "welcome":
            self.next_button.setText("Next")
            self.next_button.setEnabled(True)
        elif current_page_name == "summary":
            self.next_button.setText("Begin Installation")
            # Enable only if all required configurations are marked complete
            all_required_complete = all(self.config_state.get(key, False) for key in self.required_configs)
            self.next_button.setEnabled(all_required_complete)
        elif current_page_name == "progress":
            self.next_button.setText("Installing...")
            self.next_button.setEnabled(False)
        elif current_page_name == "finished":
            self.next_button.setVisible(False)
        else:
             # Should be unreachable
             self.next_button.setText("Next")
             self.next_button.setEnabled(False)