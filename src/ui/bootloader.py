# centrio_installer/ui/bootloader.py

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from .base import BaseConfigurationPage

class BootloaderPage(BaseConfigurationPage):
    """Page for Bootloader Configuration (Placeholder)."""
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(title="Bootloader Configuration", subtitle="Confirm bootloader installation", main_window=main_window, overlay_widget=overlay_widget, **kwargs)
        self.bootloader_enabled = True # Default to enabled

        # --- UI Elements ---
        # Boot mode section
        mode_group = Adw.PreferencesGroup(title="Boot Mode")
        mode_group.set_description("Select the boot mode for the system")
        self.add(mode_group)
        
        # Information section
        info_group = Adw.PreferencesGroup(title="Boot Information")
        info_group.set_description("Current boot configuration and requirements")
        self.add(info_group)
        
        # Button section
        button_group = Adw.PreferencesGroup()
        self.add(button_group)

        self.enable_switch_row = Adw.SwitchRow(
            title="Install Bootloader",
            subtitle="A bootloader (GRUB2) will be installed by default"
        )
        self.enable_switch_row.set_active(self.bootloader_enabled) # Set initial state
        self.enable_switch_row.connect("notify::active", self.on_enable_toggled)
        mode_group.add(self.enable_switch_row)

        # Informational text instead of non-functional advanced options
        info_group = Adw.PreferencesGroup()
        info_group.set_description("Bootloader installation will use default settings. Advanced configuration can be done post-installation.")
        self.add(info_group)
        # Add a label for padding or specific info if needed
        info_label = Gtk.Label(label="Default location and settings will be used based on detected hardware (BIOS/UEFI).")
        info_label.set_margin_top(6)
        info_label.set_margin_bottom(6)
        info_group.add(info_label)

        # --- Confirmation Button ---
        button_group = Adw.PreferencesGroup()
        self.add(button_group)
        self.complete_button = Gtk.Button(label="Confirm Bootloader Choice")
        self.complete_button.set_halign(Gtk.Align.CENTER)
        self.complete_button.set_margin_top(18)
        self.complete_button.set_margin_bottom(8)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.add_css_class("compact")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        # Button is always sensitive now
        self.complete_button.set_sensitive(True)
        button_group.add(self.complete_button)

        # UI elements are always sensitive as there's no D-Bus dependency
        self.enable_switch_row.set_sensitive(True)

    def connect_and_fetch_data(self):
        # No data fetching needed for this placeholder page
        print("BootloaderPage: No data fetching required.")
        pass

    def on_enable_toggled(self, switch, param):
        self.bootloader_enabled = switch.get_active()
        print(f"Bootloader install choice toggled: {'Install' if self.bootloader_enabled else 'Skip'}")
        if self.bootloader_enabled:
            self.enable_switch_row.set_subtitle("A bootloader (GRUB2) will be installed by default")
        else:
            self.enable_switch_row.set_subtitle("Bootloader installation will be skipped (System may not be bootable!)")

    def apply_settings_and_return(self, button):
        # No D-Bus proxy check needed
        mode_str = "Install" if self.bootloader_enabled else "Skip"
        print(f"Confirming Bootloader choice: {mode_str}")

        # Simulate applying settings - just record the choice
        self.complete_button.set_sensitive(False) # Briefly disable

        try:
            # No actual D-Bus call, just prepare the configuration dictionary
            config_values = {
                "install_bootloader": self.bootloader_enabled,
                # Add other placeholder info if needed
            }
            print(f"Bootloader choice '{mode_str}' confirmed.")
            self.show_toast(f"Bootloader choice confirmed: {mode_str}")
            super().mark_complete_and_return(button, config_values=config_values)

        except Exception as e:
            # Handle potential errors in super() or other logic, though unlikely here
            print(f"ERROR: Unexpected error during bootloader confirmation: {e}")
            self.show_toast(f"Unexpected error: {e}")
            self.complete_button.set_sensitive(True) # Re-enable on error 