# centrio_installer/ui/base.py

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

# Fixed import - use absolute import
import backend

class BaseConfigurationPage(Adw.PreferencesPage):
    """Base class for configuration ui with common functionality."""
    
    def __init__(self, title, subtitle="", main_window=None, overlay_widget=None, **kwargs):
        super().__init__(title=title, **kwargs)
        self.main_window = main_window
        self.overlay_widget = overlay_widget # For toasts
        
        # Optional subtitle if provided
        if subtitle:
            self.set_description(subtitle)
            
        # Control expansion to prevent window from being too tall
        self.set_vexpand(False)  # Don't expand vertically
        self.set_hexpand(True)

    def show_toast(self, message, timeout=3):
        """Show a toast notification if overlay is available."""
        if self.overlay_widget and hasattr(self.overlay_widget, 'add_toast'):
            toast = Adw.Toast.new(message)
            toast.set_timeout(timeout)
            self.overlay_widget.add_toast(toast)
        else:
            print(f"Toast: {message}")

    def mark_complete_and_return(self, button, config_values=None):
        """Mark this configuration as complete and return to summary."""
        if self.main_window:
            # Extract page name from class name or use a provided key
            page_key = self._get_page_key()
            if page_key:
                # Mark as complete and pass config values
                self.main_window.mark_config_complete(page_key, True, config_values)
                # Navigate back to summary
                self.main_window.return_to_summary()
            else:
                print("Warning: Could not determine page key for completion marking.")
        else:
            print("Warning: No main_window reference available for marking completion.")

    def _get_page_key(self):
        """Extract the page key from the class name."""
        class_name = self.__class__.__name__
        if class_name.endswith('Page'):
            # Convert CamelCase to lowercase (e.g., KeyboardPage -> keyboard)
            key = class_name[:-4].lower()  # Remove 'Page' suffix
            return key
        return None

    def connect_and_fetch_data(self):
        """Override in subclasses to fetch/initialize data."""
        pass

    def apply_settings_and_return(self, button):
        """Override in subclasses to apply settings."""
        pass 