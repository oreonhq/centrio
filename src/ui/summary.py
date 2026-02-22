# centrio_installer/ui/summary.py

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw


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
        self._add_config_row(storage_group, "bootloader", "Bootloader Configuration", "Boot manager installation settings", True)

        # --- Software Group ---
        # user creation is handled by gnome-initial-setup on first boot
        software_group = Adw.PreferencesGroup(
            title="Software Selection",
            description="Package groups and application configuration"
        )
        self.add(software_group)
        self._add_config_row(software_group, "payload", "Software Packages", "Package selection and repositories", True)

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
        
        if completed_count == total_required:
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