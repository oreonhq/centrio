# centrio_installer/ui/keyboard.py

import gi
import subprocess # For localectl
import re         # For parsing localectl status
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from .base import BaseConfigurationPage
# Import layout list getter
from utils import ana_get_keyboard_layouts
# Removed D-Bus imports

class KeyboardPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(title="Keyboard Layout", subtitle="Select your keyboard layout", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        # Removed D-Bus proxy variable
        self.current_vc_keymap = "us" # Default
        self.current_x_layout = "us"  # Default
        self.layout_list = []

        # --- Populate Layout List ---
        # ana_get_keyboard_layouts() returns [(display_name, keymap_code), ...]
        self.layout_pairs = ana_get_keyboard_layouts()
        self.layout_codes = [code for _, code in self.layout_pairs]
        self.layout_display_names = [name for name, _ in self.layout_pairs]

        # --- Add Keyboard Widgets --- 
        key_group = Adw.PreferencesGroup()
        self.add(key_group)
        model = Gtk.StringList.new(self.layout_display_names)
        self.layout_row = Adw.ComboRow(title="Keyboard Layout", model=model)
        key_group.add(self.layout_row)
        
        test_row = Adw.ActionRow(title="Test your keyboard settings")
        test_entry = Gtk.Entry()
        test_entry.set_placeholder_text("Type here to test layout...")
        test_row.add_suffix(test_entry)
        test_row.set_activatable_widget(test_entry)
        key_group.add(test_row)

        # --- Confirmation Button --- 
        button_group = Adw.PreferencesGroup()
        self.add(button_group) 
        self.complete_button = Gtk.Button(label="Apply Keyboard Layout")
        self.complete_button.set_halign(Gtk.Align.CENTER)
        self.complete_button.set_margin_top(24)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        # Enable based on layout list availability
        self.complete_button.set_sensitive(bool(self.layout_pairs))
        self.layout_row.set_sensitive(bool(self.layout_pairs))
        if not self.layout_pairs:
             self.layout_row.set_subtitle("Failed to load layouts")
        button_group.add(self.complete_button)
        
        # --- Fetch Current Settings --- 
        self.connect_and_fetch_data()
            
    def connect_and_fetch_data(self):
        """Fetches current keymap settings using localectl status."""
        print("Fetching keyboard settings using localectl...")
        try:
            cmd = ["localectl", "status"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
            output = result.stdout
            print(f"localectl status output:\n{output}")

            # Parse VC Keymap
            vc_match = re.search(r"VC Keymap: (\S+)", output)
            if vc_match:
                self.current_vc_keymap = vc_match.group(1)
                print(f"  Found VC Keymap: {self.current_vc_keymap}")
            else:
                print("  Could not parse VC Keymap.")

            # Parse X11 Layout
            x11_match = re.search(r"X11 Layout: (\S+)", output)
            if x11_match:
                self.current_x_layout = x11_match.group(1)
                print(f"  Found X11 Layout: {self.current_x_layout}")
            else:
                print("  Could not parse X11 Layout.")

            # Set UI selection based on fetched data (prefer VC map for console focus)
            initial_layout = self.current_vc_keymap
            if initial_layout and initial_layout in self.layout_codes:
                 try:
                     idx = self.layout_codes.index(initial_layout)
                     self.layout_row.set_selected(idx)
                 except ValueError:
                     print(f"Warning: Initial layout '{initial_layout}' not found in list.")
                     if self.layout_codes:
                         self.layout_row.set_selected(0)
            elif self.layout_codes:
                 self.layout_row.set_selected(0)

        except FileNotFoundError:
            print("ERROR: localectl command not found.")
            self.show_toast("Error: localectl not found. Cannot fetch or set keyboard settings.")
            self.layout_row.set_sensitive(False)
            self.complete_button.set_sensitive(False)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: localectl status failed: {e}\n{e.stderr}")
            self.show_toast(f"Error getting keyboard settings: {e.stderr}")
            # Allow setting even if status fails?
        except subprocess.TimeoutExpired:
            print("ERROR: localectl status command timed out.")
            self.show_toast("Getting keyboard settings timed out.")
        except Exception as e:
            print(f"ERROR: Unexpected error fetching keyboard settings: {e}")
            self.show_toast("An unexpected error occurred fetching keyboard settings.")
            
    def apply_settings_and_return(self, button):
        """Applies the selected keyboard layout using localectl."""
        selected_idx = self.layout_row.get_selected()
        if not self.layout_codes or selected_idx < 0 or selected_idx >= len(self.layout_codes):
            self.show_toast("Invalid keyboard layout selection.")
            return

        selected_layout = self.layout_codes[selected_idx]
            
        print(f"Attempting to set Keyboard Layout to '{selected_layout}' using localectl...")
        self.complete_button.set_sensitive(False) # Disable button during operation
        
        # Command to set the Virtual Console keymap
        # We prioritize setting the console keymap for an installer environment.
        # Setting X11 might also be needed depending on context, but start with VC.
        cmd = ["localectl", "set-keymap", selected_layout]
        
        try:
            print(f"  Executing: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            print(f"  localectl set-keymap output: {result.stdout}")
            print("  Keyboard layout set successfully.")
            self.show_toast(f"Keyboard layout set to '{selected_layout}' successfully!")
            
            # Pass selected layout back to main window
            config_values = {"layout": selected_layout}
            super().mark_complete_and_return(button, config_values=config_values)
            
        except FileNotFoundError:
            print("ERROR: localectl command not found.")
            self.show_toast("Error: localectl command not found. Cannot set keymap.")
            self.complete_button.set_sensitive(True) # Re-enable button on failure
        except subprocess.CalledProcessError as e:
            print(f"ERROR: localectl set-keymap failed (Exit code: {e.returncode}):")
            print(f"Stderr: {e.stderr}")
            print(f"Stdout: {e.stdout}")
            error_msg = e.stderr.strip() or f"localectl failed with exit code {e.returncode}"
            self.show_toast(f"Error setting keyboard layout: {error_msg}")
            self.complete_button.set_sensitive(True) 
        except subprocess.TimeoutExpired:
            print("ERROR: localectl set-keymap command timed out.")
            self.show_toast("Setting keyboard layout timed out.")
            self.complete_button.set_sensitive(True) 
        except Exception as e:
            print(f"ERROR: Unexpected error applying keyboard settings: {e}")
            self.show_toast(f"Unexpected error setting keyboard layout: {e}")
            self.complete_button.set_sensitive(True) 