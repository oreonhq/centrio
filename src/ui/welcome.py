# centrio_installer/ui/welcome.py

import os
import sys
import gettext
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from utils import get_os_release_info

# Translation function: always bound so no UnboundLocalError
_locale_dir = Path(__file__).resolve().parents[2] / "locale"
try:
    _t = gettext.translation("centrio", localedir=str(_locale_dir), fallback=True)
    _ = _t.gettext
except Exception:
    _ = lambda s: s


class WelcomePage(Gtk.Box):
    def __init__(self, main_window=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, **kwargs)
        self.main_window = main_window

        self.selected_language = "en_US"
        
        # Get OS Name for branding
        os_info = get_os_release_info()
        distro_name = os_info.get("NAME", "Oreon")
        
        # Set smaller margins for better screen fit
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(18)
        self.set_margin_end(18)
        
        # Create more compact content
        main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_content.set_halign(Gtk.Align.CENTER)
        main_content.set_valign(Gtk.Align.CENTER)
        main_content.set_size_request(450, -1)
        
        # Package icon (changed to proper package box icon)
        icon = Gtk.Image.new_from_icon_name("system-software-install-symbolic")
        icon.set_pixel_size(72)  # Even smaller icon
        icon.add_css_class("dim-label")
        main_content.append(icon)
        
        # Title
        title = Gtk.Label(label=_("Welcome to {}").format(distro_name))
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        main_content.append(title)

        # Description
        description = Gtk.Label(label=_("Set up your new operating system in a few simple steps."))
        description.add_css_class("title-4")
        description.add_css_class("dim-label")
        description.set_halign(Gtk.Align.CENTER)
        description.set_wrap(True)
        main_content.append(description)
        
        # Language selection - more compact
        lang_group = Adw.PreferencesGroup(title=_("Language"))
        self.lang_row = Adw.ComboRow(title=_("Installer Language"))
        
        # Comprehensive language list with proper codes
        lang_model = Gtk.StringList()
        languages = [
            ("English (US)", "en_US"),
            ("English (UK)", "en_GB"),
            ("Español", "es_ES"),
            ("Français", "fr_FR"),
            ("Deutsch", "de_DE"),
            ("Italiano", "it_IT"),
            ("Português (Brasil)", "pt_BR"),
            ("Português (Portugal)", "pt_PT"),
            ("Русский", "ru_RU"),
            ("中文 (简体)", "zh_CN"),
            ("中文 (繁體)", "zh_TW"),
            ("日本語", "ja_JP"),
            ("한국어", "ko_KR"),
            ("العربية", "ar_SA"),
            ("हिन्दी", "hi_IN"),
            ("ไทย", "th_TH"),
            ("Türkçe", "tr_TR"),
            ("Polski", "pl_PL"),
            ("Nederlands", "nl_NL"),
            ("Svenska", "sv_SE"),
            ("Norsk", "no_NO"),
            ("Dansk", "da_DK"),
            ("Suomi", "fi_FI"),
            ("Čeština", "cs_CZ"),
            ("Slovenčina", "sk_SK"),
            ("Magyar", "hu_HU"),
            ("Română", "ro_RO"),
            ("Български", "bg_BG"),
            ("Hrvatski", "hr_HR"),
            ("Slovenščina", "sl_SI"),
            ("Eesti", "et_EE"),
            ("Latviešu", "lv_LV"),
            ("Lietuvių", "lt_LT"),
            ("Ελληνικά", "el_GR"),
            ("Català", "ca_ES"),
            ("Galego", "gl_ES"),
            ("Euskara", "eu_ES"),
            ("Gaeilge", "ga_IE"),
            ("Cymraeg", "cy_GB")
        ]
        
        self.language_codes = [code for _name, code in languages]
        for name, _code in languages:
            lang_model.append(name)
        
        self.lang_row.set_model(lang_model)
        
        # Try to detect current system language
        current_lang = self._detect_current_language()
        if current_lang in self.language_codes:
            try:
                idx = self.language_codes.index(current_lang)
                self.lang_row.set_selected(idx)
            except ValueError:
                self.lang_row.set_selected(0)  # Default to English
        else:
            self.lang_row.set_selected(0)  # Default to English
            
        self.lang_row.connect("notify::selected", self.on_language_changed)
        
        lang_group.add(self.lang_row)
        main_content.append(lang_group)
        
        # Compact system info
        system_group = Adw.PreferencesGroup(title=_("Installation Overview"))
        version = os_info.get("VERSION", "10")
        version_row = Adw.ActionRow(
            title=_("Operating System"),
            subtitle=f"{distro_name} {version}"
        )
        version_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        version_row.add_prefix(version_icon)
        system_group.add(version_row)

        install_row = Adw.ActionRow(
            title=_("Installation Type"),
            subtitle=_("Full desktop with applications")
        )
        install_icon = Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        install_row.add_prefix(install_icon)
        system_group.add(install_row)

        time_row = Adw.ActionRow(
            title=_("Estimated Time"),
            subtitle=_("15-30 minutes")
        )
        time_icon = Gtk.Image.new_from_icon_name("alarm-symbolic")
        time_row.add_prefix(time_icon)
        main_content.append(system_group)

        footer_label = Gtk.Label(
            label=_("Click Next to begin configuration."),
            justify=Gtk.Justification.CENTER
        )
        footer_label.add_css_class("dim-label")
        footer_label.set_wrap(True)
        main_content.append(footer_label)
        
        # Add the main content to this box
        self.append(main_content)

    def on_language_changed(self, combo_row, pspec):
        """Handle language selection: save locale and restart installer so full UI is translated."""
        selected = combo_row.get_selected()
        if selected < 0 or selected >= len(self.language_codes):
            return
        lang_code = self.language_codes[selected]
        self.selected_language = lang_code

        # Write chosen locale so main.py applies it on next run
        lang_file = getattr(self.main_window, "installer_lang_file", None)
        script = getattr(self.main_window, "installer_script", None)
        if not lang_file or not script:
            return
        try:
            locale_value = f"{lang_code}.UTF-8" if "." not in lang_code else lang_code
            with open(lang_file, "w", encoding="utf-8") as f:
                f.write(locale_value)
        except Exception as e:
            print(f"Could not write installer language file: {e}")
            return

        # Restart the installer so gettext/locale apply to the whole UI
        root = self.get_root()
        dialog = Gtk.MessageDialog(
            transient_for=root,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=_("Language Selected"),
            secondary_text=_("The installer will restart to apply the new language.")
        )
        def on_ok(dlg, resp):
            dlg.destroy()
            try:
                os.execv(sys.executable, [sys.executable, script] + sys.argv[1:])
            except Exception as e:
                print(f"Could not restart installer: {e}")
        dialog.connect("response", on_ok)
        dialog.present()
    
    def _detect_current_language(self):
        """Detect the current system language."""
        try:
            import subprocess
            import os
            
            # First try to get from environment
            lang = os.environ.get('LANG', '')
            if lang:
                # Extract language code (e.g., "en_US.UTF-8" -> "en_US")
                lang_code = lang.split('.')[0]
                return lang_code
            
            # Fallback to localectl
            result = subprocess.run(["localectl", "status"], 
                                  capture_output=True, text=True, check=True)
            output = result.stdout
            
            # Parse System Locale
            import re
            locale_match = re.search(r"System Locale: LANG=(\S+)", output)
            if locale_match:
                lang = locale_match.group(1)
                lang_code = lang.split('.')[0]
                return lang_code
                
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        # Default fallback
        return "en_US" 