"""Sensor panel — live readouts that populate dynamically from MQTT."""

from __future__ import annotations

import time

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class SensorPanel(QWidget):
    """Auto-populating sensor readout panel.

    Rows are added on-the-fly as MQTT messages arrive, grouped by driver
    name (the third segment of the topic).
    """

    def __init__(self) -> None:
        super().__init__()
        self._groups: dict[str, _DriverGroup] = {}
        self._labels: dict[str, QLabel] = {}
        self._ts_labels: dict[str, QLabel] = {}

        # Scrollable container
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.addStretch()
        self._scroll.setWidget(self._content)

        outer = QVBoxLayout(self)
        outer.addWidget(self._scroll)

    # ── public API called from MainWindow ──────────────────────

    def update_reading(self, topic: str, payload: dict) -> None:
        value = payload.get("value")
        ts = payload.get("ts")

        if topic not in self._labels:
            self._add_row(topic)

        if value is None:
            return

        label = self._labels[topic]
        if isinstance(value, float):
            label.setText(f"{value:.4g}")
        else:
            label.setText(str(value))

        if ts is not None and topic in self._ts_labels:
            age = time.time() - ts
            self._ts_labels[topic].setText(f"{age:.0f}s ago")

    # ── internal ───────────────────────────────────────────────

    def _add_row(self, topic: str) -> None:
        # topic: ets/sensors/{driver}/{channel...}
        parts = topic.replace("ets/sensors/", "", 1).split("/", 1)
        driver = parts[0]
        channel = parts[1] if len(parts) > 1 else "?"

        if driver not in self._groups:
            group = _DriverGroup(driver)
            self._groups[driver] = group
            # Insert before the stretch
            self._layout.insertWidget(
                self._layout.count() - 1, group.box
            )

        grp = self._groups[driver]
        val_label, ts_label = grp.add_channel(channel)
        self._labels[topic] = val_label
        self._ts_labels[topic] = ts_label


class _DriverGroup:
    """A QGroupBox holding a grid of channel readouts for one driver."""

    def __init__(self, name: str) -> None:
        self.box = QGroupBox(name)
        self._grid = QGridLayout()
        self.box.setLayout(self._grid)
        self._row = 0

    def add_channel(self, channel: str) -> tuple[QLabel, QLabel]:
        name_lbl = QLabel(channel)
        name_lbl.setMinimumWidth(120)

        val_lbl = QLabel("---")
        val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val_lbl.setMinimumWidth(100)
        val_lbl.setStyleSheet("font-weight: bold;")

        ts_lbl = QLabel("")
        ts_lbl.setStyleSheet("color: gray; font-size: 10px;")
        ts_lbl.setMinimumWidth(60)

        self._grid.addWidget(name_lbl, self._row, 0)
        self._grid.addWidget(val_lbl, self._row, 1)
        self._grid.addWidget(ts_lbl, self._row, 2)
        self._row += 1

        return val_lbl, ts_lbl
