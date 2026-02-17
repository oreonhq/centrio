# centrio_installer/ui/payload.py

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .base import BaseConfigurationPage

# Default package groups and packages
DEFAULT_PACKAGE_GROUPS = {
    "core": {
        "name": "Core System",
        "description": "Essential system packages (required)",
        "packages": ["@core", "kernel", "grub2-efi-x64", "grub2-efi-x64-modules", "grub2-pc", "grub2-common", "grub2-tools", "shim-x64", "shim", "efibootmgr", "NetworkManager", "systemd-resolved", "flatpak", "xdg-desktop-portal", "xdg-desktop-portal-gtk"],
        "required": True,
        "selected": True
    },
    "desktop": {
        "name": "Additional Desktop Software",
        "description": "Extra desktop applications and utilities (desktop environment already included)",
        "packages": ["gnome-tweaks", "gnome-extensions-app"],
        "required": False,
        "selected": False
    },
    "multimedia": {
        "name": "Multimedia Support",
        "description": "Audio, video, and graphics support",
        "packages": ["@multimedia"],
        "required": False,
        "selected": True
    },
    "development": {
        "name": "Development Tools",
        "description": "Programming languages and development utilities",
        "packages": ["gcc", "make", "git", "python3", "python3-pip", "nodejs", "npm"],
        "required": False,
        "selected": False
    },
    "productivity": {
        "name": "Productivity Suite",
        "description": "Office applications and productivity tools",
        "packages": ["thunderbird"],
        "flatpak_packages": ["org.libreoffice.LibreOffice", "org.mozilla.firefox"],
        "required": False,
        "selected": True
    },
    "gaming": {
        "name": "Gaming Support",
        "description": "Steam and gaming-related packages",
        "flatpak_packages": ["com.valvesoftware.Steam", "net.lutris.Lutris", "org.winehq.Wine"],
        "required": False,
        "selected": False
    }
}

# Common custom repositories
COMMON_REPOSITORIES = {
    "rpmfusion-free": {
        "name": "RPM Fusion Free",
        "description": "Additional free software packages",
        "url": "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm",
        "enabled": False
    },
    "rpmfusion-nonfree": {
        "name": "RPM Fusion Non-Free", 
        "description": "Proprietary and patent-encumbered software",
        "url": "https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm",
        "enabled": False
    },
}

class PayloadPage(BaseConfigurationPage):
    """Enhanced page for package selection and software configuration."""
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Software Selection", 
            subtitle="Choose additional software to install on the live environment", 
            main_window=main_window, 
            overlay_widget=overlay_widget, 
            **kwargs
        )
        
        # State variables
        self.package_groups = DEFAULT_PACKAGE_GROUPS.copy()
        self.custom_repositories = COMMON_REPOSITORIES.copy()
        self.flatpak_enabled = True
        self.custom_packages = []
        self.oem_packages = []
        self.oem_repo_url = ""
        
        self._build_ui()
        
    def _build_ui(self):
        """Build the enhanced package selection UI."""
        
        # Installation Method Section
        self.method_section = Adw.PreferencesGroup(
            title="Installation Method",
            description="The system will copy the live environment to disk and install additional software"
        )
        self.add(self.method_section)
        
        # Live environment copy info
        self.live_copy_info = Adw.ActionRow(
            title="Live Environment Copy",
            subtitle="Copy the entire live system to disk"
        )
        info_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        info_icon.add_css_class("success")
        self.live_copy_info.add_prefix(info_icon)
        self.method_section.add(self.live_copy_info)
        
        # Additional Software Section
        self.additional_section = Adw.PreferencesGroup(
            title="Additional Software",
            description="Select extra applications and packages to install"
        )
        self.add(self.additional_section)
        
        self._populate_package_groups()
        
        # Flatpak Support Section
        self.flatpak_section = Adw.PreferencesGroup(
            title="Application Store",
            description="Configure Flatpak support for additional applications"
        )
        self.add(self.flatpak_section)
        
        self.flatpak_row = Adw.SwitchRow(
            title="Enable Flatpak Support",
            subtitle="Install Flatpak and add Flathub repository"
        )
        self.flatpak_row.set_active(self.flatpak_enabled)
        self.flatpak_row.connect("notify::active", self.on_flatpak_toggled)
        self.flatpak_section.add(self.flatpak_row)
        
        # Custom Repositories Section
        self.repos_section = Adw.PreferencesGroup(
            title="Additional Repositories",
            description="Enable additional software repositories"
        )
        self.add(self.repos_section)
        
        self._populate_repositories()
        
        # OEM/Custom Software Section
        self.oem_section = Adw.PreferencesGroup(
            title="OEM &amp; Custom Software",
            description="Add custom repositories and packages"
        )
        self.add(self.oem_section)
        
        # Custom repository URL
        self.oem_repo_row = Adw.EntryRow(
            title="Custom Repository URL"
        )
        self.oem_repo_row.connect("changed", self.on_oem_repo_changed)
        self.oem_section.add(self.oem_repo_row)
        
        # Custom packages
        self.custom_packages_row = Adw.EntryRow(
            title="Additional Packages"
        )
        self.custom_packages_row.connect("changed", self.on_custom_packages_changed)
        self.oem_section.add(self.custom_packages_row)
        
        # Advanced Options (Expandable)
        self.advanced_section = Adw.PreferencesGroup(
            title="Advanced Options",
            description="Expert configuration options"
        )
        self.add(self.advanced_section)
        
        # Package cache option
        self.cache_row = Adw.SwitchRow(
            title="Keep Package Cache",
            subtitle="Preserve downloaded packages for faster reinstallation"
        )
        self.cache_row.set_active(False)
        self.advanced_section.add(self.cache_row)
        
        # Confirm button
        self.button_section = Adw.PreferencesGroup()
        self.add(self.button_section)
        
        confirm_row = Adw.ActionRow(
            title="Confirm Software Selection",
            subtitle="Review and apply your additional software choices"
        )
        self.complete_button = Gtk.Button(label="Apply Software Plan")
        self.complete_button.set_valign(Gtk.Align.CENTER)
        self.complete_button.add_css_class("suggested-action")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        confirm_row.add_suffix(self.complete_button)
        self.button_section.add(confirm_row)
        
    def _populate_package_groups(self):
        """Populate the package groups section."""
        for group_id, group_info in self.package_groups.items():
            row = Adw.SwitchRow(
                title=group_info["name"],
                subtitle=group_info["description"]
            )
            
            if group_info["required"]:
                row.set_sensitive(False)
                row.set_subtitle(group_info["description"] + " (required)")
            
            row.set_active(group_info["selected"])
            row.connect("notify::active", self.on_group_toggled, group_id)
            self.additional_section.add(row)
            
    def _populate_repositories(self):
        """Populate the repositories section."""
        for repo_id, repo_info in self.custom_repositories.items():
            row = Adw.SwitchRow(
                title=repo_info["name"],
                subtitle=repo_info["description"]
            )
            row.set_active(repo_info["enabled"])
            row.connect("notify::active", self.on_repo_toggled, repo_id)
            self.repos_section.add(row)
            
    def on_group_toggled(self, switch_row, pspec, group_id):
        """Handle package group toggle."""
        is_active = switch_row.get_active()
        if group_id in self.package_groups:
            self.package_groups[group_id]["selected"] = is_active
            print(f"Package group '{group_id}' {'enabled' if is_active else 'disabled'}")
            
    def on_repo_toggled(self, switch_row, pspec, repo_id):
        """Handle repository toggle."""
        is_active = switch_row.get_active()
        if repo_id in self.custom_repositories:
            self.custom_repositories[repo_id]["enabled"] = is_active
            print(f"Repository '{repo_id}' {'enabled' if is_active else 'disabled'}")
            
    def on_flatpak_toggled(self, switch_row, pspec):
        """Handle Flatpak toggle."""
        self.flatpak_enabled = switch_row.get_active()
        print(f"Flatpak support {'enabled' if self.flatpak_enabled else 'disabled'}")
        
    def on_oem_repo_changed(self, entry_row):
        """Handle custom repository URL change."""
        self.oem_repo_url = entry_row.get_text().strip()
        print(f"Custom repository URL: {self.oem_repo_url}")
        
    def on_custom_packages_changed(self, entry_row):
        """Handle custom packages list change."""
        text = entry_row.get_text().strip()
        self.custom_packages = [pkg.strip() for pkg in text.split() if pkg.strip()]
        print(f"Custom packages: {self.custom_packages}")
        
    def on_minimal_toggled(self, switch_row, pspec):
        """Handle minimal installation toggle."""
        is_minimal = switch_row.get_active()
        
        # Disable group selections if minimal is enabled
        for i in range(self.additional_section.get_row_at_index(0) is not None and 10 or 0):
            row = self.additional_section.get_row_at_index(i)
            if row and hasattr(row, 'set_sensitive'):
                # Don't disable required groups
                group_ids = list(self.package_groups.keys())
                if i < len(group_ids):
                    group_id = group_ids[i]
                    if not self.package_groups[group_id]["required"]:
                        row.set_sensitive(not is_minimal)
        
        print(f"Minimal installation {'enabled' if is_minimal else 'disabled'}")
        
    def _get_selected_packages(self):
        """Get the complete list of DNF packages and flatpak packages to install."""
        dnf_packages = []
        flatpak_packages = []
        
        # Add packages from selected groups
        for group_id, group_info in self.package_groups.items():
            if group_info["selected"] or group_info["required"]:
                dnf_packages.extend(group_info.get("packages", []))
                # Add flatpak packages if the group has them
                if "flatpak_packages" in group_info:
                    flatpak_packages.extend(group_info["flatpak_packages"])
        
        # Add custom packages (assume they are DNF packages)
        dnf_packages.extend(self.custom_packages)
        
        # Add OEM packages if any (assume they are DNF packages)
        dnf_packages.extend(self.oem_packages)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_dnf_packages = []
        for pkg in dnf_packages:
            if pkg not in seen:
                seen.add(pkg)
                unique_dnf_packages.append(pkg)
                
        seen = set()
        unique_flatpak_packages = []
        for pkg in flatpak_packages:
            if pkg not in seen:
                seen.add(pkg)
                unique_flatpak_packages.append(pkg)
                
        return unique_dnf_packages, unique_flatpak_packages
        
    def _get_enabled_repositories(self):
        """Get the list of repositories to enable."""
        enabled_repos = []
        
        for repo_id, repo_info in self.custom_repositories.items():
            if repo_info["enabled"]:
                enabled_repos.append({
                    "id": repo_id,
                    "name": repo_info["name"],
                    "url": repo_info["url"]
                })
        
        # Add custom OEM repository if provided
        if self.oem_repo_url:
            enabled_repos.append({
                "id": "oem_custom",
                "name": "OEM Custom Repository",
                "url": self.oem_repo_url
            })
            
        return enabled_repos
        
    def apply_settings_and_return(self, button):
        """Apply the software configuration and return to summary."""
        print(f"--- Apply Software Settings START ---")
        
        selected_packages, flatpak_packages = self._get_selected_packages()
        enabled_repos = self._get_enabled_repositories()
        
        print(f"  Selected packages ({len(selected_packages)}): {selected_packages[:10]}{'...' if len(selected_packages) > 10 else ''}")
        print(f"  Flatpak packages ({len(flatpak_packages)}): {flatpak_packages}")
        print(f"  Enabled repositories: {[r['id'] for r in enabled_repos]}")
        print(f"  Flatpak enabled: {self.flatpak_enabled}")
        
        # Build configuration data
        config_values = {
            "package_groups": {gid: ginfo["selected"] for gid, ginfo in self.package_groups.items()},
            "packages": selected_packages,
            "flatpak_packages": flatpak_packages,
            "repositories": enabled_repos,
            "flatpak_enabled": self.flatpak_enabled,
            "custom_packages": self.custom_packages,
            "oem_repo_url": self.oem_repo_url,
            "keep_cache": self.cache_row.get_active(),
            "use_live_copy": True  # Always use live copy
        }
        
        # Show confirmation
        package_count = len(selected_packages)
        flatpak_count = len(flatpak_packages)
        repo_count = len(enabled_repos)
        features = []
        
        if self.flatpak_enabled:
            features.append("Flatpak")
        if self.custom_packages:
            features.append(f"{len(self.custom_packages)} custom packages")
            
        feature_text = f" ({', '.join(features)})" if features else ""
        
        total_software = package_count + flatpak_count
        self.show_toast(f"Software plan: {total_software} additional packages ({package_count} DNF, {flatpak_count} Flatpak), {repo_count} repositories{feature_text}")
        
        print("Software configuration confirmed. Returning to summary.")
        super().mark_complete_and_return(button, config_values=config_values) 