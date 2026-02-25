# centrio_installer/ui/payload.py

import os
import subprocess
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .base import BaseConfigurationPage

_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'icons')


def _detect_nvidia_gpu():
    """Return True if an NVIDIA GPU is detected via lspci (no drivers required)."""
    try:
        r = subprocess.run(
            ["lspci", "-n"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and "10de:" in r.stdout  # NVIDIA PCI vendor ID
    except Exception:
        return False

# Default package groups and packages
DEFAULT_PACKAGE_GROUPS = {
    "core": {
        "name": "Core System",
        "description": "Essential system packages (required)",
        "packages": ["@core", "kernel", "grub2-efi-x64", "grub2-efi-x64-modules", "grub2-pc", "grub2-common", "grub2-tools", "shim-x64", "shim", "efibootmgr", "flatpak", "xdg-desktop-portal", "xdg-desktop-portal-gtk"],
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
        "flatpak_packages": ["org.libreoffice.LibreOffice"],
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
        self.package_group_rows = {}
        self.custom_repositories = COMMON_REPOSITORIES.copy()
        self.flatpak_enabled = True
        self.nvidia_drivers = _detect_nvidia_gpu()
        self.server_install = False  # False=Desktop, True=Server
        self.custom_packages = []
        self.oem_packages = []
        self.oem_repo_url = ""
        
        self.network_warning_row = None
        self._build_ui()

    def refresh_for_network(self, network_config=None):
        """Gray out network-dependent options when no network. Call when page is shown."""
        net = network_config or {}
        connected = net.get("network_status") == "connected"
        skip = net.get("skip_network", True)
        has_network = connected and not skip
        for w in [self.flatpak_row, self.flatpak_section, self.repos_section, self.oem_section, self.oem_repo_row, self.custom_packages_row]:
            w.set_sensitive(has_network)
        if not has_network:
            self.browser_section.set_sensitive(False)
        for gid, ginfo in self.package_groups.items():
            if not ginfo["required"] and gid in self.package_group_rows:
                self.package_group_rows[gid].set_sensitive(has_network)
        if hasattr(self, "network_warning_group"):
            self.network_warning_group.set_visible(not has_network)
        # Re-apply Flatpak-dependent graying when network is available
        if has_network:
            self._refresh_flatpak_dependent()

    def _build_ui(self):
        """Build the enhanced package selection UI."""
        
        self.network_warning_group = Adw.PreferencesGroup(
            title="No Network",
            description="Network is required for these options"
        )
        self.network_warning_row = Adw.ActionRow(
            title="Network required",
            subtitle="Configure network in Network Settings to enable additional software, Flatpak, repositories, and custom packages."
        )
        warn_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        warn_icon.add_css_class("error")
        self.network_warning_row.add_prefix(warn_icon)
        self.network_warning_group.add(self.network_warning_row)
        self.network_warning_group.set_visible(False)
        self.add(self.network_warning_group)

        # Installation Type: Desktop vs Server
        self.install_type_section = Adw.PreferencesGroup(
            title="Installation Type",
            description="Choose desktop or server installation"
        )
        self.add(self.install_type_section)
        self.desktop_server_radios = {}
        first_radio = None
        for opt_id, label in [("desktop", "Desktop Installation"), ("server", "Server Installation")]:
            row = Adw.ActionRow(title=label)
            radio = Gtk.CheckButton()
            if first_radio is None:
                first_radio = radio
                radio.set_active(True)
            else:
                radio.set_group(first_radio)
            radio.connect("toggled", self._on_install_type_toggled, opt_id)
            row.add_suffix(radio)
            row.set_activatable_widget(radio)
            self.install_type_section.add(row)
            self.desktop_server_radios[opt_id] = (row, radio)

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

        # Web Browser Section (all Flatpak)
        self.browser_section = Adw.PreferencesGroup(
            title="Web Browser",
            description="Select a web browser to install"
        )
        self.add(self.browser_section)
        BROWSERS = [
            ("firefox", "Firefox", "org.mozilla.firefox", "firefox.svg"),
            ("chrome", "Chrome", "com.google.Chrome", "chrome.svg"),
            ("brave", "Brave", "com.brave.Browser", "brave.svg"),
            ("edge", "Edge", "com.microsoft.Edge", "edge.svg"),
            ("none", "No web browser", None, None),
        ]
        self.browser_options = {b[0]: {"name": b[1], "flatpak": b[2], "icon_file": b[3]} for b in BROWSERS}
        self.selected_browser = "none"
        self.browser_rows = {}
        self.browser_radios = {}
        first_radio = None
        first_bid = None
        for bid, binfo in self.browser_options.items():
            row = Adw.ActionRow(title=binfo["name"], subtitle="You can always install one later" if bid == "none" else "")
            if binfo["icon_file"]:
                path = os.path.join(_ICONS_DIR, binfo["icon_file"])
                icon = Gtk.Image.new_from_file(path) if os.path.isfile(path) else Gtk.Image()
            else:
                icon = Gtk.Image.new_from_icon_name("window-close-symbolic")
            row.add_prefix(icon)
            radio = Gtk.CheckButton()
            if first_radio is None:
                first_radio = radio
                first_bid = bid
                radio.set_active(True)
            else:
                radio.set_group(first_radio)
            radio.connect("toggled", self._on_browser_selected, bid)
            row.add_suffix(radio)
            row.set_activatable_widget(radio)
            self.browser_section.add(row)
            self.browser_rows[bid] = row
            self.browser_radios[bid] = radio
        if first_bid:
            self.selected_browser = first_bid  # Sync state with default selected radio

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
        
        # NVIDIA Drivers (Oreon ships NVIDIA repo)
        self.nvidia_row = Adw.SwitchRow(
            title="NVIDIA Drivers",
            subtitle="Install dkms-nvidia, nvidia-driver, nvidia-driver-cuda"
        )
        self.nvidia_row.set_active(self.nvidia_drivers)
        self.nvidia_row.connect("notify::active", self._on_nvidia_toggled)
        self.advanced_section.add(self.nvidia_row)

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
        self.complete_button.add_css_class("compact")
        self.complete_button.connect("clicked", self.apply_settings_and_return)
        confirm_row.add_suffix(self.complete_button)
        self.button_section.add(confirm_row)

        self._refresh_flatpak_dependent()
        self._refresh_server_dependent()
        
    def _populate_package_groups(self):
        """Populate the package groups section."""
        for group_id, group_info in self.package_groups.items():
            subtitle = group_info["description"]
            if group_info["required"] and "(required)" not in subtitle:
                subtitle = subtitle + " (required)"
            row = Adw.SwitchRow(
                title=group_info["name"],
                subtitle=subtitle
            )
            self.package_group_rows[group_id] = row
            if group_info["required"]:
                row.set_sensitive(False)
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
        if group_id not in self.package_groups:
            return
        is_active = switch_row.get_active()
        if self.package_groups[group_id]["selected"] == is_active:
            return  # No change, avoid fighting with programmatic set_active
        self.package_groups[group_id]["selected"] = is_active
        print(f"Package group '{group_id}' {'enabled' if is_active else 'disabled'}")
            
    def on_repo_toggled(self, switch_row, pspec, repo_id):
        """Handle repository toggle."""
        is_active = switch_row.get_active()
        if repo_id in self.custom_repositories:
            self.custom_repositories[repo_id]["enabled"] = is_active
            print(f"Repository '{repo_id}' {'enabled' if is_active else 'disabled'}")
            
    def _on_browser_selected(self, radio, bid):
        if radio.get_active():
            self.selected_browser = bid
            print(f"Browser selected: {bid}")

    def _on_install_type_toggled(self, radio, opt_id):
        if radio.get_active():
            self.server_install = opt_id == "server"
            self._refresh_server_dependent()
            print(f"Installation type: {'Server' if self.server_install else 'Desktop'}")

    def _on_nvidia_toggled(self, switch_row, pspec):
        self.nvidia_drivers = switch_row.get_active()
        print(f"NVIDIA drivers: {'enabled' if self.nvidia_drivers else 'disabled'}")

    def _refresh_server_dependent(self):
        """Gray out bundle and browser options when Server installation is selected."""
        desktop = not self.server_install
        self.additional_section.set_sensitive(desktop)
        if self.server_install:
            for gid, row in self.package_group_rows.items():
                if not self.package_groups[gid].get("required"):
                    self.package_groups[gid]["selected"] = False
                    row.set_active(False)
            self.selected_browser = "none"
            for bid, radio in self.browser_radios.items():
                radio.set_active(bid == "none")
            self.flatpak_enabled = False
            self.flatpak_row.set_active(False)
            self.browser_section.set_sensitive(False)
            self.flatpak_section.set_sensitive(False)
        else:
            # Desktop: restore Flatpak and sensitivities
            self.flatpak_enabled = True
            self.flatpak_row.set_active(True)
            self.flatpak_section.set_sensitive(True)
            self.browser_section.set_sensitive(True)
            self._refresh_flatpak_dependent()

    def _refresh_flatpak_dependent(self):
        """Gray out Flatpak-dependent options when Flatpak is disabled."""
        enabled = self.flatpak_enabled and not self.server_install
        self.browser_section.set_sensitive(enabled)
        for gid, ginfo in self.package_groups.items():
            if ginfo.get("flatpak_packages") and gid in self.package_group_rows:
                row = self.package_group_rows[gid]
                row.set_sensitive(enabled)
                if not enabled:
                    self.package_groups[gid]["selected"] = False
                    row.set_active(False)

    def on_flatpak_toggled(self, switch_row, pspec):
        """Handle Flatpak toggle."""
        self.flatpak_enabled = switch_row.get_active()
        self._refresh_flatpak_dependent()
        if not self.flatpak_enabled:
            self.selected_browser = "none"
            for bid, radio in self.browser_radios.items():
                radio.set_active(bid == "none")
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
        
        # Add packages from selected groups (skip bundles when server install)
        for group_id, group_info in self.package_groups.items():
            if self.server_install and not group_info.get("required"):
                continue
            if group_info["selected"] or group_info["required"]:
                dnf_packages.extend(group_info.get("packages", []))
                # Add flatpak packages only when Flatpak enabled and desktop
                if not self.server_install and self.flatpak_enabled and "flatpak_packages" in group_info:
                    flatpak_packages.extend(group_info["flatpak_packages"])
        
        # Add selected browser (Flatpak only) when Flatpak enabled and desktop
        if not self.server_install and self.flatpak_enabled and self.selected_browser != "none":
            fp = self.browser_options.get(self.selected_browser, {}).get("flatpak")
            if fp:
                flatpak_packages.append(fp)
        
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
        # Sync selected browser from active radio (handles default Firefox when user never toggled)
        for bid, radio in self.browser_radios.items():
            if radio.get_active():
                self.selected_browser = bid
                break
        net = self.main_window.final_config.get("network", {}) if self.main_window else {}
        has_network = net.get("network_status") == "connected" and not net.get("skip_network", True)
        selected_packages, flatpak_packages = self._get_selected_packages()
        if not has_network:
            flatpak_packages = []
            custom_set = set(self.custom_packages) | set(self.oem_packages)
            selected_packages = [p for p in selected_packages if p not in custom_set]
        enabled_repos = [] if not has_network else self._get_enabled_repositories()
        flatpak_enabled_effective = self.flatpak_enabled and has_network
        
        print(f"  Selected packages ({len(selected_packages)}): {selected_packages[:10]}{'...' if len(selected_packages) > 10 else ''}")
        print(f"  Flatpak packages ({len(flatpak_packages)}): {flatpak_packages}")
        print(f"  Enabled repositories: {[r['id'] for r in enabled_repos]}")
        print(f"  Flatpak enabled: {self.flatpak_enabled}")
        
        # Add NVIDIA driver packages if enabled
        if self.nvidia_drivers:
            selected_packages = list(selected_packages) + ["dkms-nvidia", "nvidia-driver", "nvidia-driver-cuda"]

        # Build configuration data
        config_values = {
            "package_groups": {gid: ginfo["selected"] for gid, ginfo in self.package_groups.items()},
            "packages": selected_packages,
            "flatpak_packages": flatpak_packages,
            "repositories": enabled_repos,
            "flatpak_enabled": flatpak_enabled_effective,
            "nvidia_drivers": self.nvidia_drivers,
            "server_install": self.server_install,
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
        
        if flatpak_enabled_effective:
            features.append("Flatpak")
        if self.custom_packages:
            features.append(f"{len(self.custom_packages)} custom packages")
            
        feature_text = f" ({', '.join(features)})" if features else ""
        
        total_software = package_count + flatpak_count
        self.show_toast(f"Software plan: {total_software} additional packages ({package_count} DNF, {flatpak_count} Flatpak), {repo_count} repositories{feature_text}")
        
        print("Software configuration confirmed. Returning to summary.")
        super().mark_complete_and_return(button, config_values=config_values) 