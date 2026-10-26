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
# centrio_installer/ui/summary.py

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

import accounts


class SummaryPage(Adw.PreferencesPage):
    def __init__(self, main_window, **kwargs):
        super().__init__(title="Installation Summary", **kwargs)
        self.main_window = main_window
        self.config_rows = {}
        
        # Fix scrolling conflicts with main window
        self.set_vexpand(False)
        self.set_hexpand(True)

        # --- Header Section ---
        header_group = Adw.PreferencesGroup()
        self.add(header_group)
        
        status_row = Adw.ActionRow(
            title="Installation Configuration",
            subtitle="Review and complete all required settings before proceeding"
        )
        status_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        status_row.add_prefix(status_icon)
        header_group.add(status_row)

        # --- Localization Group ---
        loc_group = Adw.PreferencesGroup(
            title="Localization &amp; Input",
            description="Language, keyboard, and regional settings"
        )
        self.add(loc_group)
        self._add_config_row(loc_group, "keyboard", "Keyboard Layout", "Configure keyboard input method", True)
        self._add_config_row(loc_group, "language", "System Language", "Set the default system locale", False)

        # --- System Configuration Group ---
        sys_group = Adw.PreferencesGroup(
            title="System Configuration", 
            description="Core system and hardware settings"
        )
        self.add(sys_group)
        self._add_config_row(sys_group, "timedate", "Time &amp; Date", "Timezone and time synchronization", True)
        self._add_config_row(sys_group, "network", "Network Connectivity", "Network configuration for additional software", True)

        # --- Storage Group ---
        storage_group = Adw.PreferencesGroup(
            title="Storage &amp; Installation Target",
            description="Disk partitioning and filesystem configuration"
        )
        self.add(storage_group)
        self._add_config_row(storage_group, "disk", "Installation Destination", "Disk selection and partitioning method", True)

        # --- Software Group ---
        # user creation is handled by gnome-initial-setup on first boot
        software_group = Adw.PreferencesGroup(
            title="Software Selection",
            description="Package groups and application configuration"
        )
        self.add(software_group)
        self._add_config_row(software_group, "payload", "Software Packages", "Package selection and repositories", True)

        self._build_account_group()

        # --- Installation Ready Status ---
        self.status_group = Adw.PreferencesGroup(
            title="Installation Status"
        )
        self.add(self.status_group)
        
        self.ready_status_row = Adw.ActionRow(
            title="Configuration Status",
            subtitle="Complete required settings to proceed with installation"
        )
        self.status_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.ready_status_row.add_prefix(self.status_icon)
        self.status_group.add(self.ready_status_row)

    def _build_account_group(self):
        self.account_touched = set()
        self.account_group = Adw.PreferencesGroup(
            title="Server Accounts",
            description="Server installs have no setup wizard. Create a user, enable root, or both."
        )
        self.add(self.account_group)

        self.user_switch = Adw.SwitchRow(title="Create a user account")
        self.user_switch.set_active(True)
        self.account_group.add(self.user_switch)
        self.full_name_row = Adw.EntryRow(title="Full name")
        self.account_group.add(self.full_name_row)
        self.username_row = Adw.EntryRow(title="Username")
        self.account_group.add(self.username_row)
        self.username_error = self._make_error_label()
        self.user_password_row = Adw.PasswordEntryRow(title="Password")
        self.account_group.add(self.user_password_row)
        self.user_confirm_row = Adw.PasswordEntryRow(title="Confirm password")
        self.account_group.add(self.user_confirm_row)
        self.user_password_error = self._make_error_label()
        self.admin_row = Adw.ActionRow(title="Administrator", subtitle="Add this user to the wheel group")
        self.admin_check = Gtk.CheckButton()
        self.admin_check.set_active(True)
        self.admin_check.set_valign(Gtk.Align.CENTER)
        self.admin_row.add_suffix(self.admin_check)
        self.admin_row.set_activatable_widget(self.admin_check)
        self.account_group.add(self.admin_row)

        self.root_switch = Adw.SwitchRow(title="Enable the root account")
        self.root_switch.set_active(False)
        self.account_group.add(self.root_switch)
        self.root_password_row = Adw.PasswordEntryRow(title="Root password")
        self.account_group.add(self.root_password_row)
        self.root_confirm_row = Adw.PasswordEntryRow(title="Confirm root password")
        self.account_group.add(self.root_confirm_row)
        self.root_password_error = self._make_error_label()
        self.general_error = self._make_error_label()

        for row, name in (
            (self.full_name_row, "full_name"),
            (self.username_row, "username"),
            (self.user_password_row, "user_password"),
            (self.user_confirm_row, "user_password"),
            (self.root_password_row, "root_password"),
            (self.root_confirm_row, "root_password"),
        ):
            row.connect("changed", self._on_account_field_changed, name)
        self.user_switch.connect("notify::active", self._on_account_switch_changed)
        self.root_switch.connect("notify::active", self._on_account_switch_changed)
        self.admin_check.connect("toggled", lambda *_: self.main_window.update_navigation())
        self._refresh_account_group()

    def _make_error_label(self):
        label = Gtk.Label(xalign=0, wrap=True)
        label.add_css_class("error")
        label.add_css_class("caption")
        label.set_margin_top(6)
        label.set_margin_start(12)
        label.set_visible(False)
        self.account_group.add(label)
        return label

    def is_server_install(self):
        payload = self.main_window.final_config.get("payload") or {}
        return bool(payload.get("server_install", False))

    def _account_errors(self):
        return accounts.validate_accounts(
            self.user_switch.get_active(),
            self.username_row.get_text(),
            self.user_password_row.get_text(),
            self.user_confirm_row.get_text(),
            self.root_switch.get_active(),
            self.root_password_row.get_text(),
            self.root_confirm_row.get_text(),
        )

    def accounts_ready(self):
        if not self.is_server_install():
            return True
        return not self._account_errors()

    def build_accounts_config(self):
        return accounts.build_accounts_config(
            self.user_switch.get_active(),
            self.full_name_row.get_text().strip(),
            self.username_row.get_text(),
            self.user_password_row.get_text(),
            self.admin_check.get_active(),
            self.root_switch.get_active(),
            self.root_password_row.get_text(),
        )

    def _on_account_field_changed(self, row, name):
        self.account_touched.add(name)
        self._refresh_account_group()
        self.main_window.update_navigation()

    def _on_account_switch_changed(self, *_args):
        self._refresh_account_group()
        self.main_window.update_navigation()

    def _refresh_account_group(self):
        server = self.is_server_install()
        self.account_group.set_visible(server)
        user_on = self.user_switch.get_active()
        root_on = self.root_switch.get_active()
        for row in (self.full_name_row, self.username_row, self.user_password_row, self.user_confirm_row, self.admin_row):
            row.set_visible(user_on)
        for row in (self.root_password_row, self.root_confirm_row):
            row.set_visible(root_on)
        errors = self._account_errors() if server else {}
        self._show_error(self.username_error, errors.get("username"), user_on and "username" in self.account_touched)
        self._show_error(self.user_password_error, errors.get("user_password"), user_on and "user_password" in self.account_touched)
        self._show_error(self.root_password_error, errors.get("root_password"), root_on and "root_password" in self.account_touched)
        self._show_error(self.general_error, errors.get("general"), True)
        if hasattr(self, "ready_status_row"):
            self._update_installation_status()

    def _show_error(self, label, message, allowed):
        visible = bool(message) and allowed
        label.set_text(message or "")
        label.set_visible(visible)
        for row in self._rows_for_error(label):
            if visible:
                row.add_css_class("error")
            else:
                row.remove_css_class("error")

    def _rows_for_error(self, label):
        if label is self.username_error:
            return (self.username_row,)
        if label is self.user_password_error:
            return (self.user_password_row, self.user_confirm_row)
        if label is self.root_password_error:
            return (self.root_password_row, self.root_confirm_row)
        return ()

    def _add_config_row(self, group, key, title, subtitle_base, required):
        row = Adw.ActionRow(title=title)
        row.set_activatable(True)
        row.connect("activated", self.on_row_activated, key)
        
        # Initialize config_state in main window if not already present
        if key not in self.main_window.config_state:
             self.main_window.config_state[key] = False
             
        self.config_rows[key] = {
            "row": row, 
            "required": required, 
            "subtitle_base": subtitle_base,
            "icon_widget": None 
        }
        group.add(row)
        
        # Update row status based on initial state from main_window
        self.update_row_status(key, self.main_window.config_state.get(key, False))

    def on_row_activated(self, row, key):
        self.main_window.navigate_to_config(key)

    def update_row_status(self, key, is_complete):
        if key not in self.config_rows:
            print(f"Warning: Attempted to update status for unknown row key: {key}")
            return
            
        config = self.config_rows[key]
        row = config["row"]
        subtitle = config["subtitle_base"]
        icon_name = None
        new_icon_widget = None

        # Determine icon and subtitle based on state
        if is_complete:
            row.set_subtitle(f"{subtitle} (Configured)")
            icon_name = "object-select-symbolic"
            row.add_css_class("success")
            row.remove_css_class("warning")
        elif config["required"]:
            row.set_subtitle(f"{subtitle} (Required)")
            icon_name = "dialog-warning-symbolic"
            row.add_css_class("warning")
            row.remove_css_class("success")
        else:
            row.set_subtitle(f"{subtitle} (Optional)")
            row.remove_css_class("warning")
            row.remove_css_class("success")

        # Remove the previous icon widget if it exists
        if config["icon_widget"]:
            try:
                 row.remove(config["icon_widget"])
            except Exception as e:
                 print(f"Warning: Failed to remove previous icon for row '{key}': {e}")
            config["icon_widget"] = None
        
        # Add the new icon if one is needed
        if icon_name:
            new_icon_widget = Gtk.Image.new_from_icon_name(icon_name)
            row.add_suffix(new_icon_widget)
            config["icon_widget"] = new_icon_widget
            
        # Update overall installation status
        if hasattr(self, "account_group"):
            self._refresh_account_group()
        self._update_installation_status()
    
    def _update_installation_status(self):
        """Update the overall installation readiness status."""
        # Only update if the status row has been created
        if not hasattr(self, 'ready_status_row') or not self.ready_status_row:
            return
            
        required_keys = [key for key, config in self.config_rows.items() if config["required"]]
        completed_required = [key for key in required_keys if self.main_window.config_state.get(key, False)]
        
        total_required = len(required_keys)
        completed_count = len(completed_required)
        
        if completed_count == total_required and not self.accounts_ready():
            self.ready_status_row.set_title("Accounts Required")
            self.ready_status_row.set_subtitle("Set up a server user account or a root password")
            self.status_icon.set_from_icon_name("dialog-warning-symbolic")
            self.ready_status_row.add_css_class("warning")
            self.ready_status_row.remove_css_class("success")
        elif completed_count == total_required:
            # All required items completed
            self.ready_status_row.set_title("Ready for Installation")
            self.ready_status_row.set_subtitle(f"All required settings configured ({completed_count}/{total_required})")
            self.status_icon.set_from_icon_name("object-select-symbolic")
            self.ready_status_row.add_css_class("success")
            self.ready_status_row.remove_css_class("warning")
        else:
            # Still missing required items
            missing_count = total_required - completed_count
            self.ready_status_row.set_title("Configuration Incomplete")
            self.ready_status_row.set_subtitle(f"{missing_count} required setting(s) remaining ({completed_count}/{total_required})")
            self.status_icon.set_from_icon_name("dialog-warning-symbolic")
            self.ready_status_row.add_css_class("warning")
            self.ready_status_row.remove_css_class("success")