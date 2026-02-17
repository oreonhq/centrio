# centrio_installer/ui/language.py

import gi
import subprocess # For localectl
import re         # For parsing localectl status
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from .base import BaseConfigurationPage
# Use locale list getter
from utils import ana_get_available_locales
# Removed D-Bus imports

class LanguagePage(BaseConfigurationPage): # Renamed class slightly
    def __init__(self, main_window, overlay_widget, **kwargs):
        # Changed title and subtitle to reflect setting system locale
        super().__init__(title="System Language", subtitle="Select the primary language for the installed system", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        # Removed D-Bus proxy variable
        self.available_locales = {} # Dict: {code: display_name}
        self.current_locale = "en_US.UTF-8" # Default
        self.locale_codes = [] # List of codes for ComboRow model

        # --- Populate Locales List ---
        self.available_locales = ana_get_available_locales()
        self.locale_codes = list(self.available_locales.keys())
        self.locale_display_names = [self.available_locales.get(c, c) for c in self.locale_codes]

        # --- Add Widgets using self.add() --- 
        lang_group = Adw.PreferencesGroup(title="System Locale")
        self.add(lang_group)

        locale_model = Gtk.StringList.new(self.locale_display_names)
        self.locale_row = Adw.ComboRow(title="Locale", model=locale_model)
        lang_group.add(self.locale_row)

        # --- Confirmation Button --- 
        button_group = Adw.PreferencesGroup()
        self.add(button_group)
        self.complete_button = Gtk.Button(label="Apply System Locale")
        self.complete_button.set_halign(Gtk.Align.CENTER)
        self.complete_button.set_margin_top(24)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        # Sensitivity depends on available locales
        self.complete_button.set_sensitive(bool(self.available_locales))
        self.locale_row.set_sensitive(bool(self.available_locales))
        if not self.available_locales:
             self.locale_row.set_subtitle("Failed to load locales")
        button_group.add(self.complete_button)

        # --- Fetch Current Settings --- 
        self.connect_and_fetch_data() 

    def connect_and_fetch_data(self):
        """Fetches current system locale using localectl status."""
        print("Fetching locale settings using localectl...")
        try:
            cmd = ["localectl", "status"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
            output = result.stdout
            print(f"localectl status output:\n{output}")

            # Parse System Locale (LANG=...)
            locale_match = re.search(r"System Locale: LANG=(\S+)", output)
            if locale_match:
                self.current_locale = locale_match.group(1)
                print(f"  Found System Locale: {self.current_locale}")
            else:
                print("  Could not parse System Locale from localectl output.")

            # Update UI based on fetched value
            if self.current_locale in self.locale_codes:
                try:
                    idx = self.locale_codes.index(self.current_locale)
                    self.locale_row.set_selected(idx)
                except ValueError:
                    print(f"Warning: Fetched locale '{self.current_locale}' not in list.")
                    if self.locale_codes: self.locale_row.set_selected(0)
            elif self.locale_codes:
                 self.locale_row.set_selected(0) # Default to first if fetch failed/not found

        except FileNotFoundError:
            print("ERROR: localectl command not found.")
            self.show_toast("Error: localectl not found. Cannot get/set locale settings.")
            self.locale_row.set_sensitive(False)
            self.complete_button.set_sensitive(False)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: localectl status failed: {e}\n{e.stderr}")
            self.show_toast(f"Error getting locale settings: {e.stderr}")
        except subprocess.TimeoutExpired:
            print("ERROR: localectl status command timed out.")
            self.show_toast("Getting locale settings timed out.")
        except Exception as e:
            print(f"ERROR: Unexpected error fetching locale settings: {e}")
            self.show_toast(f"An unexpected error occurred fetching locale settings.")

    def apply_settings_and_return(self, button):
        """Applies the selected system locale using localectl."""
        selected_idx = self.locale_row.get_selected()
        if not self.locale_codes or selected_idx < 0 or selected_idx >= len(self.locale_codes):
             self.show_toast("Invalid locale selection.")
             return
             
        selected_locale = self.locale_codes[selected_idx]
            
        print(f"Attempting to set System Locale to '{selected_locale}' using localectl...")
        self.complete_button.set_sensitive(False) 
        
        # Command to set the system locale (LANG variable)
        cmd = ["localectl", "set-locale", f"LANG={selected_locale}"]
        
        try:
            print(f"  Executing: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            print(f"  localectl set-locale output: {result.stdout}")
            print("  System locale set successfully.")
            self.show_toast(f"System locale set to '{selected_locale}' successfully!")
            
            # Pass selected locale back
            config_values = {"locale": selected_locale}
            super().mark_complete_and_return(button, config_values=config_values)
            
        except FileNotFoundError:
            print("ERROR: localectl command not found.")
            self.show_toast("Error: localectl command not found. Cannot set locale.")
            self.complete_button.set_sensitive(True) 
        except subprocess.CalledProcessError as e:
            print(f"ERROR: localectl set-locale failed (Exit code: {e.returncode}):")
            print(f"Stderr: {e.stderr}")
            print(f"Stdout: {e.stdout}")
            error_msg = e.stderr.strip() or f"localectl failed with exit code {e.returncode}"
            self.show_toast(f"Error setting system locale: {error_msg}")
            self.complete_button.set_sensitive(True) 
        except subprocess.TimeoutExpired:
            print("ERROR: localectl set-locale command timed out.")
            self.show_toast("Setting system locale timed out.")
            self.complete_button.set_sensitive(True) 
        except Exception as e:
            print(f"ERROR: Unexpected error applying system locale: {e}")
            self.show_toast(f"Unexpected error setting system locale: {e}")
            self.complete_button.set_sensitive(True) 