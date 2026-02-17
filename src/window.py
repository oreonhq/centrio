# centrio_installer/window.py
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    from gi.repository import Gtk, Adw, GLib  # type: ignore
except ImportError:
    print("Warning: gi.repository could not be imported")
    raise

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
from ui.user import UserPage
from ui.payload import PayloadPage
from ui.bootloader import BootloaderPage

class CentrioInstallerWindow(Adw.ApplicationWindow):
    def __init__(self, installer_script=None, **kwargs):
        super().__init__(**kwargs)
        self.installer_script = installer_script or sys.argv[0]

        self.config_state = {} # Stores completion status (True/False) for each config key
        self.required_configs = set() # Set of keys for required configurations
        self.main_page_order = ["welcome", "summary", "progress", "finished"]
        # All known configuration page keys
        self.config_page_keys = ["keyboard", "language", "timedate", "disk", "network", "user", "payload", "bootloader"]
        self.final_config = {} # Stores final selected values passed back from ui

        self.set_title("Centrio Installer")
        self.set_default_size(700, 350)  # Much smaller default height
        self.set_resizable(True)
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        # --- Create the single Toast Overlay --- 
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_vexpand(True)
        main_box.append(self.toast_overlay)

        # --- View Stack (directly in the overlay) --- 
        self.view_stack = Adw.ViewStack()
        self.toast_overlay.set_child(self.view_stack)

        # --- Add ui to the stack ---
        # Main flow ui (pass main_window so welcome can restart for language change)
        self.welcome_page = WelcomePage(main_window=self)
        self.view_stack.add_titled(self.welcome_page, self.main_page_order[0], "Welcome")

        # Create Summary Page - this will also populate config_state and required_configs
        self.summary_page = SummaryPage(main_window=self)
        self.view_stack.add_titled(self.summary_page, self.main_page_order[1], "Installation Summary")

        self.progress_page = ProgressPage()
        self.view_stack.add_titled(self.progress_page, self.main_page_order[2], "Installation Progress")

        self.finished_page = FinishedPage(app=self.get_application())
        self.view_stack.add_titled(self.finished_page, self.main_page_order[3], "Finished")

        # Configuration ui - Pass main_window and the overlay
        self.keyboard_page = KeyboardPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.keyboard_page, "keyboard", "Keyboard Settings")
        
        self.language_page = LanguagePage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.language_page, "language", "Language Settings")
        
        self.timedate_page = TimeDatePage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.timedate_page, "timedate", "Time & Date Settings")
        
        self.disk_page = DiskPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.disk_page, "disk", "Disk Settings")
        
        self.network_page = NetworkConnectivityPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.network_page, "network", "Network Settings")
        
        self.user_page = UserPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.user_page, "user", "User Settings")
        
        self.payload_page = PayloadPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.payload_page, "payload", "Payload Settings")
        
        self.bootloader_page = BootloaderPage(main_window=self, overlay_widget=self.toast_overlay)
        self.view_stack.add_titled(self.bootloader_page, "bootloader", "Bootloader Settings")
        
        # Ensure required_configs is populated based on SummaryPage rows
        # (Should be done within SummaryPage._add_config_row now)
        for key, config in self.summary_page.config_rows.items():
            if config["required"]:
                self.required_configs.add(key)
            # Ensure config_state has an entry for every row added
            if key not in self.config_state:
                self.config_state[key] = False 

        # --- Navigation Buttons (below the overlay) --- 
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nav_box.set_halign(Gtk.Align.END)
        nav_box.set_margin_top(6)
        nav_box.set_margin_bottom(6)
        nav_box.set_margin_start(12)
        nav_box.set_margin_end(12)
        main_box.append(nav_box)

        self.abort_button = Gtk.Button(label="Abort")
        self.abort_button.add_css_class("destructive-action")
        self.abort_button.connect("clicked", self.exit_window)
        nav_box.append(self.abort_button)

        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", self.go_back)
        nav_box.append(self.back_button)

        self.next_button = Gtk.Button(label="Next")
        self.next_button.add_css_class("suggested-action")
        self.next_button.connect("clicked", self.go_next)
        nav_box.append(self.next_button)

        # Update navigation state when the visible child changes
        self.view_stack.connect("notify::visible-child-name", self.update_navigation)
        # Set initial navigation state
        self.update_navigation()

    def get_current_page_info(self):
        """Helper to get information about the currently visible page."""
        current_page_name = self.view_stack.get_visible_child_name()
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
        GLib.idle_add(self.view_stack.set_visible_child_name, page_name)

    def navigate_to_config(self, config_key):
        """Navigates to a specific configuration page by its key."""
        if config_key in self.config_page_keys:
            self.navigate_to_page(config_key)

    def return_to_summary(self):
        """Navigates back to the summary page."""
        self.navigate_to_page("summary")

    def mark_config_complete(self, key, is_complete, config_values=None):
        """Mark config as complete, store final values ONCE, and update summary UI."""
        if key in self.config_state:
            print(f">>> mark_config_complete called for key='{key}', is_complete={is_complete}, has_config_values={config_values is not None}")
            
            # Check if we are newly marking as complete or re-marking
            was_already_complete = self.config_state.get(key, False)
            self.config_state[key] = is_complete
            
            # Only store config_values the FIRST time it's marked complete
            if is_complete and config_values is not None and not was_already_complete:
                 # Store the actual configuration data passed back
                 self.final_config[key] = config_values
                 print(f"  Stored final config for '{key}': {config_values}")
            elif is_complete and config_values is not None and was_already_complete:
                 print(f"  Skipping config storage for '{key}' (already marked complete).")
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
                # Pass final config to installation using idle_add for safety
                GLib.idle_add(self.progress_page.start_installation, self, self.final_config) 
            elif main_index < len(self.main_page_order) - 1:
                # --- Navigate to Next Main Page --- 
                next_page_name = self.main_page_order[main_index + 1]
                print(f"Navigating from '{current_page_name}' to '{next_page_name}'")
                self.navigate_to_page(next_page_name)
            elif current_page_name == "finished": # Should be handled by finished page button
                 print("'Next' clicked on finished page - Quitting.")
                 self.get_application().quit()
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
        sys.exit(0)

    def update_navigation(self, stack=None, param=None):
        """Update the state of back/next buttons based on the current page."""
        # Use idle_add to prevent issues if called during stack transitions
        GLib.idle_add(self._update_navigation_idle)

    def _update_navigation_idle(self):
        """Actual navigation update logic, called via GLib.idle_add."""
        current_page_name, is_main_page, is_config_page, main_index = self.get_current_page_info()

        if not current_page_name:
             # Should not happen, but handle defensively
             self.back_button.set_sensitive(False)
             self.next_button.set_sensitive(False)
             return

        # --- Back Button Logic --- 
        if is_config_page:
            self.back_button.set_sensitive(True)
            self.back_button.set_label("Cancel")
            self.back_button.set_visible(True)
        elif is_main_page:
            self.back_button.set_label("Back")
            # Can go back if not on welcome or progress page
            can_go_back = main_index > 0 and current_page_name != "progress" and current_page_name != "finished"
            self.back_button.set_sensitive(can_go_back)
            self.back_button.set_visible(current_page_name != "finished") # Hide on finished
        else:
            # Should be unreachable if ui are named correctly
            self.back_button.set_sensitive(False)
            self.back_button.set_visible(True)

        # --- Next Button Logic --- 
        self.next_button.remove_css_class("destructive-action") # Ensure default style
        self.next_button.add_css_class("suggested-action")
        self.next_button.set_visible(True) # Assume visible unless on finished page
        
        if is_config_page:
            # Config ui handle their own primary action via their own buttons.
            # The main 'Next' button should ideally just return to summary.
            self.next_button.set_label("Return to Summary")
            self.next_button.set_sensitive(True)
            # We could hide this button and rely only on the page's button + Cancel?
            # self.next_button.set_visible(False) 
        elif current_page_name == "welcome":
            self.next_button.set_label("Next")
            self.next_button.set_sensitive(True)
        elif current_page_name == "summary":
            self.next_button.set_label("Begin Installation")
            # Enable only if all required configurations are marked complete
            all_required_complete = all(self.config_state.get(key, False) for key in self.required_configs)
            self.next_button.set_sensitive(all_required_complete)
        elif current_page_name == "progress":
            self.next_button.set_label("Installing...")
            self.next_button.set_sensitive(False) # Cannot navigate next from progress
        elif current_page_name == "finished":
            self.next_button.set_visible(False) # No next button on finished page
        else:
             # Should be unreachable
             self.next_button.set_label("Next")
             self.next_button.set_sensitive(False) 