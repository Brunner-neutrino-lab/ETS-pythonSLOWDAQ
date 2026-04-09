"""Control panel — relay buttons, setpoint controls, controller status."""

from __future__ import annotations

import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from slowcontrol.core.config import AppConfig
from slowcontrol.core.mqtt import MQTTClient


class ControlPanel(QWidget):
    """Interactive controls for relays, setpoints, and controller modes."""

    def __init__(self, config: AppConfig, mqtt: MQTTClient) -> None:
        super().__init__()
        self.config = config
        self.mqtt = mqtt
        self._status_labels: dict[str, QLabel] = {}
        self._relay_indicators: dict[str, QLabel] = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        main = QVBoxLayout(content)

        main.addWidget(self._build_relay_group())
        main.addWidget(self._build_lakeshore_group())
        main.addWidget(self._build_autovalve_group())
        main.addWidget(self._build_humidity_group())
        main.addWidget(self._build_interlocks_group())
        main.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)

    # ── relay controls ─────────────────────────────────────────

    def _build_relay_group(self) -> QGroupBox:
        box = QGroupBox("Relays")
        grid = QGridLayout()
        row = 0

        for rname in self.config.relays:
            grid.addWidget(QLabel(rname), row, 0)

            indicator = QLabel("CLOSED")
            indicator.setStyleSheet(
                "color: green; font-weight: bold; min-width: 60px;"
            )
            indicator.setAlignment(Qt.AlignCenter)
            grid.addWidget(indicator, row, 1)
            self._relay_indicators[rname] = indicator

            btn_open = QPushButton("Open")
            btn_close = QPushButton("Close")
            btn_open.clicked.connect(self._make_relay_cmd(rname, "open"))
            btn_close.clicked.connect(self._make_relay_cmd(rname, "close"))
            grid.addWidget(btn_open, row, 2)
            grid.addWidget(btn_close, row, 3)
            row += 1

        box.setLayout(grid)
        return box

    def _make_relay_cmd(self, relay: str, action: str):
        def cmd():
            self.mqtt.publish(
                f"ets/commands/relay/{relay}",
                {"action": action, "source": "gui"},
            )

        return cmd

    # ── LakeShore setpoint ─────────────────────────────────────

    def _build_lakeshore_group(self) -> QGroupBox:
        box = QGroupBox("LakeShore 350")
        layout = QHBoxLayout()

        layout.addWidget(QLabel("Setpoint (K):"))
        self._ls_spinbox = QDoubleSpinBox()
        self._ls_spinbox.setRange(0, 400)
        self._ls_spinbox.setDecimals(2)
        self._ls_spinbox.setValue(77.0)
        layout.addWidget(self._ls_spinbox)

        btn = QPushButton("Set")
        btn.clicked.connect(self._send_lakeshore_setpoint)
        layout.addWidget(btn)

        self._ls_status = QLabel("---")
        layout.addWidget(self._ls_status)
        self._status_labels["lakeshore"] = self._ls_status

        layout.addStretch()
        box.setLayout(layout)
        return box

    def _send_lakeshore_setpoint(self) -> None:
        value = self._ls_spinbox.value()
        self.mqtt.publish(
            "ets/commands/lakeshore/setpoint",
            {"value": value, "loop": 1},
        )

    # ── autovalve ──────────────────────────────────────────────

    def _build_autovalve_group(self) -> QGroupBox:
        box = QGroupBox("Autovalve")
        layout = QHBoxLayout()

        btn_auto = QPushButton("Auto")
        btn_manual = QPushButton("Manual")
        btn_auto.clicked.connect(
            lambda: self.mqtt.publish(
                "ets/commands/autovalve/mode", {"mode": "auto"}
            )
        )
        btn_manual.clicked.connect(
            lambda: self.mqtt.publish(
                "ets/commands/autovalve/mode", {"mode": "manual"}
            )
        )
        layout.addWidget(btn_auto)
        layout.addWidget(btn_manual)

        self._av_status = QLabel("WAITING")
        self._av_status.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._av_status)
        self._status_labels["autovalve"] = self._av_status

        layout.addStretch()
        box.setLayout(layout)
        return box

    # ── humidity PID ───────────────────────────────────────────

    def _build_humidity_group(self) -> QGroupBox:
        box = QGroupBox("Humidity PID")
        layout = QHBoxLayout()

        layout.addWidget(QLabel("Setpoint (%RH):"))
        self._hum_spinbox = QDoubleSpinBox()
        self._hum_spinbox.setRange(0, 100)
        self._hum_spinbox.setDecimals(1)
        self._hum_spinbox.setValue(7.2)
        layout.addWidget(self._hum_spinbox)

        btn = QPushButton("Set")
        btn.clicked.connect(self._send_humidity_setpoint)
        layout.addWidget(btn)

        self._hum_status = QLabel("---")
        layout.addWidget(self._hum_status)
        self._status_labels["humidity_pid"] = self._hum_status

        layout.addStretch()
        box.setLayout(layout)
        return box

    def _send_humidity_setpoint(self) -> None:
        value = self._hum_spinbox.value()
        self.mqtt.publish(
            "ets/commands/humidity/setpoint", {"value": value}
        )

    # ── interlocks ─────────────────────────────────────────────

    def _build_interlocks_group(self) -> QGroupBox:
        box = QGroupBox("Interlocks")
        layout = QHBoxLayout()

        self._interlock_indicator = QLabel("OK")
        self._interlock_indicator.setStyleSheet(
            "color: green; font-weight: bold; font-size: 14px;"
        )
        layout.addWidget(self._interlock_indicator)

        self._interlock_detail = QLabel("")
        self._interlock_detail.setWordWrap(True)
        layout.addWidget(self._interlock_detail)
        self._status_labels["interlocks"] = self._interlock_detail

        layout.addStretch()
        box.setLayout(layout)
        return box

    # ── MQTT status updates (called from MainWindow) ───────────

    def update_status(self, topic: str, payload: dict) -> None:
        # Relay states
        if topic.startswith("ets/status/relays/"):
            rname = topic.split("/")[-1]
            state = payload.get("state", "unknown").upper()
            indicator = self._relay_indicators.get(rname)
            if indicator:
                indicator.setText(state)
                if state == "OPEN":
                    indicator.setStyleSheet(
                        "color: red; font-weight: bold; min-width: 60px;"
                    )
                else:
                    indicator.setStyleSheet(
                        "color: green; font-weight: bold; min-width: 60px;"
                    )

        # Autovalve state
        elif topic == "ets/status/autovalve":
            state = payload.get("state", "?")
            mode = payload.get("mode", "?")
            self._av_status.setText(f"{state}  [{mode}]")

        # Humidity PID
        elif topic == "ets/status/humidity_pid":
            meas = payload.get("measured")
            duty = payload.get("duty_cycle", 0)
            txt = f"measured={meas}%  duty={duty:.1%}" if meas else "---"
            self._hum_status.setText(txt)

        # Interlocks
        elif topic == "ets/status/interlocks":
            ok = payload.get("all_ok", True)
            violations = payload.get("violations", {})
            if ok:
                self._interlock_indicator.setText("OK")
                self._interlock_indicator.setStyleSheet(
                    "color: green; font-weight: bold; font-size: 14px;"
                )
                self._interlock_detail.setText("")
            else:
                self._interlock_indicator.setText("ALERT")
                self._interlock_indicator.setStyleSheet(
                    "color: red; font-weight: bold; font-size: 14px;"
                )
                self._interlock_detail.setText(
                    "\n".join(f"{k}: {v}" for k, v in violations.items())
                )
