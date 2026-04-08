from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .base import BaseConfigurationPage


class PayloadPage(BaseConfigurationPage):
    def __init__(self, main_window, overlay_widget, **kwargs):
        super().__init__(
            title="Software Packages",
            subtitle="Package selection and repositories",
            main_window=main_window,
            overlay_widget=overlay_widget,
            use_card=False,
            **kwargs,
        )
        self.package_groups = {
            "core":    {"label": "Base system",       "packages": ["@core", "kernel"]},
            "desktop": {"label": "Desktop essentials", "packages": ["plasma-desktop", "dolphin", "konsole"]},
            "dev":     {"label": "Developer tools",   "packages": ["git", "gcc", "make", "python3-pip"]},
            "media":   {"label": "Media tools",       "packages": ["vlc", "ffmpeg", "gimp"]},
        }
        self.flatpak_catalog = {
            "org.kde.kdenlive":            "Kdenlive",
            "org.libreoffice.LibreOffice": "LibreOffice",
        }
        self.gaming_bundle = {
            "com.valvesoftware.Steam": "Steam",
            "net.lutris.Lutris":       "Lutris",
        }
        self.repo_presets = {
            "rpmfusion_free":    {"name": "RPM Fusion Free",    "url": "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release.rpm"},
            "rpmfusion_nonfree": {"name": "RPM Fusion Nonfree", "url": "https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release.rpm"},
        }

        # scrollable content area - sits directly in page_layout (the card)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("scrollContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 0)
        content_layout.setSpacing(14)

        # --- Package Groups ---
        self.groups_box = QGroupBox("Package Groups")
        groups_layout = QGridLayout(self.groups_box)
        groups_layout.setContentsMargins(14, 10, 14, 12)
        groups_layout.setHorizontalSpacing(18)
        groups_layout.setVerticalSpacing(8)
        self.group_checks = {}
        for idx, (group_id, group_info) in enumerate(self.package_groups.items()):
            cb = QCheckBox(group_info["label"])
            cb.setChecked(group_id in ("core", "desktop"))
            if group_id == "core":
                cb.setEnabled(False)
            groups_layout.addWidget(cb, idx // 2, idx % 2)
            self.group_checks[group_id] = cb
        content_layout.addWidget(self.groups_box)

        # --- Profile ---
        desktop_box = QGroupBox("Profile")
        desktop_layout = QVBoxLayout(desktop_box)
        desktop_layout.setContentsMargins(14, 10, 14, 12)
        desktop_layout.setSpacing(6)
        self.server_install = QCheckBox("Install server profile")
        self.server_install.toggled.connect(self._on_server_mode_toggled)
        self.desktop_mode = QCheckBox("Install desktop profile")
        self.desktop_mode.setChecked(True)
        self.desktop_mode.toggled.connect(self._on_desktop_mode_toggled)
        desktop_layout.addWidget(self.desktop_mode)
        desktop_layout.addWidget(self.server_install)
        content_layout.addWidget(desktop_box)

        # --- Default Browser (Flatpak only) ---
        self.browser_box = QGroupBox("Default Browser")
        browser_layout = QVBoxLayout(self.browser_box)
        browser_layout.setContentsMargins(14, 10, 14, 12)
        browser_layout.setSpacing(6)
        self.browser_group = QButtonGroup(self)
        self.browser_checks = {}
        browser_options = [
            ("org.mozilla.firefox",    "Firefox"),
            ("org.chromium.Chromium",  "Chromium"),
            ("none",                   "No browser preinstalled"),
        ]
        for app_id, label in browser_options:
            cb = QCheckBox(label)
            if app_id == "org.mozilla.firefox":
                cb.setChecked(True)
            browser_layout.addWidget(cb)
            self.browser_group.addButton(cb)
            self.browser_checks[app_id] = cb
            cb.toggled.connect(lambda checked, b=app_id: self._enforce_single_browser(b, checked))
        content_layout.addWidget(self.browser_box)

        # --- Flatpak ---
        flatpak_box = QGroupBox("Flatpak Applications")
        flatpak_layout = QVBoxLayout(flatpak_box)
        flatpak_layout.setContentsMargins(14, 10, 14, 12)
        flatpak_layout.setSpacing(6)
        self.flatpak_enabled = QCheckBox("Enable Flatpak support")
        self.flatpak_enabled.setChecked(True)
        flatpak_layout.addWidget(self.flatpak_enabled)
        self.flatpak_checks = {}
        for app_id, app_name in self.flatpak_catalog.items():
            cb = QCheckBox(app_name)
            flatpak_layout.addWidget(cb)
            self.flatpak_checks[app_id] = cb
        content_layout.addWidget(flatpak_box)

        # --- Gaming ---
        gaming_box = QGroupBox("Gaming Support")
        gaming_layout = QVBoxLayout(gaming_box)
        gaming_layout.setContentsMargins(14, 10, 14, 12)
        gaming_layout.setSpacing(6)
        self.gaming_checks = {}
        for app_id, app_name in self.gaming_bundle.items():
            cb = QCheckBox(app_name)
            gaming_layout.addWidget(cb)
            self.gaming_checks[app_id] = cb
        content_layout.addWidget(gaming_box)

        # --- Repositories ---
        repo_box = QGroupBox("Repositories")
        repo_layout = QVBoxLayout(repo_box)
        repo_layout.setContentsMargins(14, 10, 14, 12)
        repo_layout.setSpacing(6)
        self.repo_checks = {}
        for repo_id, repo in self.repo_presets.items():
            cb = QCheckBox(repo["name"])
            repo_layout.addWidget(cb)
            self.repo_checks[repo_id] = cb
        self.repo_url = QLineEdit()
        self.repo_url.setPlaceholderText("Optional OEM repository URL")
        repo_layout.addWidget(self.repo_url)
        content_layout.addWidget(repo_box)

        # --- Advanced ---
        advanced_box = QGroupBox("Advanced")
        advanced_form = QFormLayout(advanced_box)
        advanced_form.setContentsMargins(14, 10, 14, 12)
        advanced_form.setHorizontalSpacing(12)
        advanced_form.setVerticalSpacing(8)
        self.nvidia_drivers = QCheckBox("Install NVIDIA drivers")
        self.keep_cache = QCheckBox("Keep package cache")
        self.custom_packages = QLineEdit()
        self.custom_packages.setPlaceholderText("custom1 custom2")
        advanced_form.addRow(self.nvidia_drivers)
        advanced_form.addRow(self.keep_cache)
        advanced_form.addRow("Extra DNF packages", self.custom_packages)
        content_layout.addWidget(advanced_box)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        self.page_layout.addWidget(scroll, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("pageSubtitle")
        self.page_layout.addWidget(self.status_label)
        self._update_network_status_label()

        btn = QPushButton("Apply Software Configuration")
        btn.setObjectName("primaryButton")
        btn.clicked.connect(self.apply_settings_and_return)
        self.page_layout.addWidget(btn)

    def _enforce_single_browser(self, browser_key, checked):
        if not checked:
            if not any(cb.isChecked() for cb in self.browser_checks.values()):
                self.browser_checks["firefox"].setChecked(True)
            return
        for key, cb in self.browser_checks.items():
            if key != browser_key:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)

    def _on_server_mode_toggled(self, checked):
        if checked:
            self.desktop_mode.blockSignals(True)
            self.desktop_mode.setChecked(False)
            self.desktop_mode.blockSignals(False)
        self._apply_desktop_sensitivity(desktop_active=not checked)

    def _on_desktop_mode_toggled(self, checked):
        if checked:
            self.server_install.blockSignals(True)
            self.server_install.setChecked(False)
            self.server_install.blockSignals(False)
        self._apply_desktop_sensitivity(desktop_active=checked)

    def _apply_desktop_sensitivity(self, desktop_active):
        self.groups_box.setEnabled(desktop_active)
        self.browser_box.setEnabled(desktop_active)

    def _update_network_status_label(self):
        net = self.main_window.final_config.get("network", {}) if self.main_window else {}
        has_network = net.get("network_status") == "connected" and not net.get("skip_network", True)
        if has_network:
            self.status_label.setText("Network is available. Online repos and Flatpak can be enabled.")
        else:
            self.status_label.setText("Network is not configured. Online repos and Flatpak will be ignored.")

    def refresh_for_network(self, _network_config):
        self._update_network_status_label()

    def _collect_selected_packages(self):
        selected = []
        group_state = {}
        for group_id, check in self.group_checks.items():
            enabled = check.isChecked()
            group_state[group_id] = enabled
            if enabled:
                selected.extend(self.package_groups[group_id]["packages"])
        selected.extend([p for p in self.custom_packages.text().split() if p.strip()])
        return selected, group_state

    def _selected_browser_flatpak(self):
        chosen = next((k for k, cb in self.browser_checks.items() if cb.isChecked()), None)
        if chosen and chosen != "none":
            return [chosen]
        return []

    def apply_settings_and_return(self, _button=None):
        net = self.main_window.final_config.get("network", {}) if self.main_window else {}
        has_network = net.get("network_status") == "connected" and not net.get("skip_network", True)
        selected_packages, group_state = self._collect_selected_packages()
        custom = [p for p in self.custom_packages.text().split() if p.strip()]
        if not has_network:
            selected_packages = [p for p in selected_packages if p not in custom]
        repos = []
        for repo_id, check in self.repo_checks.items():
            if check.isChecked() and has_network:
                repo = self.repo_presets[repo_id]
                repos.append({"id": repo_id, "name": repo["name"], "url": repo["url"]})
        if has_network and self.repo_url.text().strip():
            repos.append({"id": "oem_custom", "name": "OEM Custom Repository", "url": self.repo_url.text().strip()})
        flatpak_packages = []
        if has_network and self.flatpak_enabled.isChecked():
            flatpak_packages = [app for app, cb in self.flatpak_checks.items() if cb.isChecked()]
            flatpak_packages += self._selected_browser_flatpak()
        flatpak_packages += [app for app, cb in self.gaming_checks.items() if cb.isChecked()]
        config_values = {
            "package_groups":  group_state,
            "packages":        selected_packages,
            "flatpak_packages": flatpak_packages,
            "repositories":    repos,
            "flatpak_enabled": bool(has_network and self.flatpak_enabled.isChecked()),
            "nvidia_drivers":  self.nvidia_drivers.isChecked(),
            "server_install":  self.server_install.isChecked(),
            "custom_packages": custom,
            "oem_repo_url":    self.repo_url.text().strip(),
            "keep_cache":      self.keep_cache.isChecked(),
            "use_live_copy":   True,
        }
        self.show_toast(
            f"Software plan saved. {len(selected_packages)} DNF packages, {len(flatpak_packages)} Flatpak apps."
        )
        self.mark_complete_and_return(config_values=config_values)
