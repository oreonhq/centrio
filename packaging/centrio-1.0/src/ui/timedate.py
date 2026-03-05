# centrio_installer/ui/timedate.py

import gi
import subprocess # For timedatectl
import re         # For parsing timedatectl output
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from .base import BaseConfigurationPage
# Use ana_get_all_regions_and_timezones from utils
from utils import ana_get_all_regions_and_timezones
# Removed D-Bus imports

class TimeDatePage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(title="Time &amp; Date", subtitle="Set timezone and time settings", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        # Removed D-Bus proxy variable
        self.current_timezone = "UTC" # Default
        self.current_ntp = False    # Default
        # Removed unused is_utc variable
        self.timezone_list = []

        # --- Populate List --- 
        self.timezone_list = ana_get_all_regions_and_timezones()
        # If pytz isn't available, the list might be very short

        # --- Add Widgets using self.add() ---
        
        time_group = Adw.PreferencesGroup()
        
        self.add(time_group)

        self.tz_search_entry = Gtk.SearchEntry(placeholder_text="Search timezones...")
        self.tz_search_entry.set_hexpand(True)
        time_group.add(self.tz_search_entry)

        # Use ComboRow with filter for timezone selection
        # StringFilter/StringFilterMode require GTK 4.10+; use CustomFilter fallback
        self.timezone_string_list = Gtk.StringList.new(self.timezone_list)
        self._tz_search_text = [""]
        def _tz_match_func(item, _data):
            s = item.get_string()
            return self._tz_search_text[0].lower() in s.lower()
        self.timezone_filter = Gtk.CustomFilter.new(_tz_match_func, None)
        self.timezone_filter_model = Gtk.FilterListModel(model=self.timezone_string_list)
        self.timezone_filter_model.set_filter(self.timezone_filter)
        self.timezone_row = Adw.ComboRow(title="Timezone", model=self.timezone_filter_model)
        self.tz_search_entry.connect("search-changed", self._on_timezone_search_changed)
        time_group.add(self.timezone_row)
        
        # NTP toggle
        self.ntp_row = Adw.SwitchRow(
            title="Enable Network Time Protocol (NTP)",
            subtitle="Automatically synchronize system time with network servers"
        )
        self.ntp_row.set_active(self.current_ntp)
        self.ntp_row.connect("notify::active", self.on_ntp_toggled)
        time_group.add(self.ntp_row)
        
        # --- Confirmation Button --- 
        button_group = Adw.PreferencesGroup()
        self.add(button_group)
        self.complete_button = Gtk.Button(label="Apply Time & Date Settings")
        self.complete_button.set_halign(Gtk.Align.CENTER)
        self.complete_button.set_margin_top(18)
        self.complete_button.set_margin_bottom(8)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.add_css_class("compact")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        # Enable based on whether timezones could be listed
        self.complete_button.set_sensitive(bool(self.timezone_list))
        self.timezone_row.set_sensitive(bool(self.timezone_list))
        self.ntp_row.set_sensitive(True) # Assume NTP can always be toggled
        if not self.timezone_list:
             self.timezone_row.set_subtitle("Failed to load timezones")
        button_group.add(self.complete_button)

        # --- Fetch Current Settings --- 
        self.connect_and_fetch_data()

    def _on_timezone_search_changed(self, entry):
        self._tz_search_text[0] = entry.get_text()
        self.timezone_filter.changed(Gtk.FilterChange.DIFFERENT)

    def on_ntp_toggled(self, switch_row, pspec):
        """Handle NTP toggle changes."""
        self.current_ntp = switch_row.get_active()
        print(f"NTP toggled to: {self.current_ntp}")

    def connect_and_fetch_data(self):
        """Fetches current timezone and NTP status using timedatectl."""
        print("Fetching time settings using timedatectl...")
        try:
            cmd = ["timedatectl", "status"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5)
            output = result.stdout
            print(f"timedatectl status output:\n{output}")

            # Parse Timezone
            tz_match = re.search(r"Time zone: ([^ ]+)", output)
            if tz_match:
                self.current_timezone = tz_match.group(1)
                print(f"  Found Timezone: {self.current_timezone}")
            else:
                print("  Could not parse timezone from timedatectl output.")
                # Keep default self.current_timezone = "UTC"

            # Parse NTP status
            ntp_match = re.search(r"NTP service: (\w+)", output)
            if ntp_match:
                self.current_ntp = (ntp_match.group(1) == "active")
                print(f"  Found NTP status: {self.current_ntp}")
            else:
                 # Older versions might use "Network time on: yes/no"
                 ntp_match_alt = re.search(r"Network time on: (yes|no)", output)
                 if ntp_match_alt:
                      self.current_ntp = (ntp_match_alt.group(1) == "yes")
                      print(f"  Found Network time status: {self.current_ntp}")
                 else:
                      print("  Could not parse NTP status from timedatectl output.")
                      # Keep default self.current_ntp = False

            # Update UI based on fetched values
            # Set Timezone Combo
            if self.current_timezone in self.timezone_list:
                try:
                    idx = self.timezone_list.index(self.current_timezone)
                    self.timezone_row.set_selected(idx)
                except ValueError:
                    print(f"Warning: Fetched timezone '{self.current_timezone}' not in list.")
                    if self.timezone_list: self.timezone_row.set_selected(0)
            elif self.timezone_list:
                self.timezone_row.set_selected(0) # Default to first if fetch failed/not found
                
            # Set NTP Switch
            self.ntp_row.set_active(self.current_ntp)
            
            # Ensure widgets are sensitive
            self.timezone_row.set_sensitive(bool(self.timezone_list))
            self.ntp_row.set_sensitive(True)
            self.complete_button.set_sensitive(bool(self.timezone_list))

        except FileNotFoundError:
            print("ERROR: timedatectl command not found.")
            self.show_toast("Error: timedatectl command not found. Cannot get/set time settings.")
            self.timezone_row.set_sensitive(False)
            self.ntp_row.set_sensitive(False)
            self.complete_button.set_sensitive(False)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: timedatectl status failed: {e}\n{e.stderr}")
            self.show_toast(f"Error getting time settings: {e.stderr}")
            # Might still be able to set, keep UI enabled for now?
        except subprocess.TimeoutExpired:
            print("ERROR: timedatectl status command timed out.")
            self.show_toast("Getting time settings timed out.")
        except Exception as e:
            print(f"ERROR: Unexpected error fetching time settings: {e}")
            self.show_toast(f"An unexpected error occurred fetching time settings.")
            
    def apply_settings_and_return(self, button):
        """Applies timezone and NTP settings using timedatectl."""
        selected_idx = self.timezone_row.get_selected()
        if selected_idx == Gtk.INVALID_LIST_POSITION or selected_idx < 0:
            self.show_toast("Invalid timezone selection.")
            return
        sel_item = self.timezone_filter_model.get_item(selected_idx)
        if not sel_item:
            self.show_toast("Invalid timezone selection.")
            return
        selected_tz = sel_item.get_string()
        network_time_enabled = self.ntp_row.get_active()
        ntp_bool_str = "true" if network_time_enabled else "false"

        print(f"Attempting to set Timezone='{selected_tz}', NTP={ntp_bool_str} using timedatectl...")
        self.complete_button.set_sensitive(False) 
        errors = []

        # 1. Set Timezone
        try:
            print(f"  Executing: timedatectl set-timezone {selected_tz}")
            cmd_tz = ["timedatectl", "set-timezone", selected_tz]
            result_tz = subprocess.run(cmd_tz, capture_output=True, text=True, check=True, timeout=5)
            print("  Timezone set successfully.")
        except FileNotFoundError:
             errors.append("timedatectl command not found")
        except subprocess.CalledProcessError as e:
             err_msg = f"Failed to set timezone: {e.stderr.strip()}"
             print(f"ERROR: {err_msg}")
             errors.append(err_msg)
        except subprocess.TimeoutExpired:
             errors.append("Setting timezone timed out")
        except Exception as e:
             errors.append(f"Unexpected error setting timezone: {e}")

        # 2. Set NTP
        # Only proceed if timezone setting didn't have critical errors like command not found
        if "timedatectl command not found" not in errors:
            try:
                print(f"  Executing: timedatectl set-ntp {ntp_bool_str}")
                cmd_ntp = ["timedatectl", "set-ntp", ntp_bool_str]
                result_ntp = subprocess.run(cmd_ntp, capture_output=True, text=True, check=True, timeout=5)
                print("  NTP setting applied successfully.")
            except subprocess.CalledProcessError as e:
                 err_msg = f"Failed to set NTP: {e.stderr.strip()}"
                 print(f"ERROR: {err_msg}")
                 # Add error, but might be non-fatal (e.g., ntp service not installed)
                 errors.append(err_msg + " (NTP service might need installation/configuration)")
            except subprocess.TimeoutExpired:
                 errors.append("Setting NTP timed out")
            except Exception as e:
                 errors.append(f"Unexpected error setting NTP: {e}")
        
        # Handle outcome
        if not errors:
            self.show_toast("Time settings applied successfully!")
            config_values = {"timezone": selected_tz, "ntp": network_time_enabled}
            super().mark_complete_and_return(button, config_values=config_values)
        else:
            # Show combined error messages
            full_error_message = "Error applying time settings: " + "; ".join(errors)
            print(f"ERROR: {full_error_message}")
            self.show_toast(full_error_message)
            self.complete_button.set_sensitive(True) # Re-enable on error 