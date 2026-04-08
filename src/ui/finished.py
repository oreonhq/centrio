import subprocess

from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class FinishedPage(QWidget):
    def __init__(self, app=None, **kwargs):
        super().__init__(**kwargs)
        self.app = app

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(14)

        title = QLabel("Installation Complete")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                "Centrio has been installed. Remove installation media and reboot your computer."
            )
        )

        reboot_button = QPushButton("Reboot Now")
        reboot_button.clicked.connect(self.on_reboot)
        layout.addWidget(reboot_button)
        layout.addStretch(1)

    def on_reboot(self):
        print("Reboot requested.")
        try:
            subprocess.run(["systemctl", "reboot"], check=True, timeout=5)
        except Exception as e:
            print(f"Reboot failed: {e}")
            if self.window():
                self.window().close()
