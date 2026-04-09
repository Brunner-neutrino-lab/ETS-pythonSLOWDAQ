"""Main window — ties the slow-control service to a PyQt5 GUI."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from slowcontrol.core.config import AppConfig
from slowcontrol.core.service import SlowControlService
from slowcontrol.gui.control_panel import ControlPanel
from slowcontrol.gui.sensor_panel import SensorPanel


class _MQTTBridge(QObject):
    """Thread-safe bridge: MQTT callback thread → Qt main thread."""

    message = pyqtSignal(str, dict)


class MainWindow(QMainWindow):
    def __init__(self, config_path: str | Path):
        super().__init__()
        self.setWindowTitle("ETS Slow Control")
        self.resize(1000, 700)

        self.config = AppConfig.from_yaml(config_path)
        self.service = SlowControlService(config_path)

        self._bridge = _MQTTBridge()
        self._bridge.message.connect(self._on_mqtt_message)

        self._build_ui()
        self._start_service()

    # ── UI construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        tabs = QTabWidget()

        self._sensor_panel = SensorPanel()
        self._control_panel = ControlPanel(self.config, self.service.mqtt)

        tabs.addTab(self._sensor_panel, "Sensors")
        tabs.addTab(self._control_panel, "Controls")

        self.setCentralWidget(tabs)

        # Status bar
        self._status_label = QLabel("Connecting…")
        self.statusBar().addPermanentWidget(self._status_label)

        self._timer = QTimer()
        self._timer.timeout.connect(self._update_status)
        self._timer.start(2000)

    # ── service management ─────────────────────────────────────

    def _start_service(self) -> None:
        self.service.start()
        self.service.mqtt.subscribe("ets/#", self._forward_to_gui)

    def _forward_to_gui(self, topic: str, payload: dict) -> None:
        self._bridge.message.emit(topic, payload)

    def _on_mqtt_message(self, topic: str, payload: dict) -> None:
        if topic.startswith("ets/sensors/"):
            self._sensor_panel.update_reading(topic, payload)
        elif topic.startswith("ets/status/"):
            self._control_panel.update_status(topic, payload)

    def _update_status(self) -> None:
        if self.service.mqtt.is_connected:
            nd = sum(
                1 for d in self.service.drivers.values() if d.is_running
            )
            nc = len(self.service.controllers)
            self._status_label.setText(
                f"Connected  |  {nd} drivers  |  {nc} controllers"
            )
        else:
            self._status_label.setText("Disconnected")

    def closeEvent(self, event) -> None:
        self.service.stop()
        super().closeEvent(event)


def run_gui(config_path: str | Path) -> None:
    app = QApplication(sys.argv)
    window = MainWindow(config_path)
    window.show()
    sys.exit(app.exec_())
