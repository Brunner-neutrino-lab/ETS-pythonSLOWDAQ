# ETS Slow Control

A unified Python-based slow control system for the ETS cryostat, replacing the
previous collection of independent scripts with a single, MQTT-centric service.

## Architecture

```
ESP32 (WiFi/MQTT) -----+
                        |
LabJack T7 (x2) ---+   |       +----------------------------------+
LakeShore 350  ----|   +-------|  Python Slow Control Service     |
Arduino DHT    ----|   |       |                                  |
RPi GPIO relays ---+   |       |  Drivers   -> MQTT publish       |--> Relays
                        |       |  MQTT sub  -> Controllers (PID)  |
                        |       |  Interlocks -> safety watchdog   |
                        +-------+----------------------------------+
                                        |
                                   Mosquitto (MQTT)
                                   +----+----+
                              Telegraf   Node-RED
                                 |       (buttons)
                             InfluxDB 2.x
                              +--+--+
                          Grafana  ETS-pythonDAQ
```

**Key principle:** MQTT is the universal message bus.  Python owns all logic.
Node-RED provides browser-accessible buttons.  Grafana provides dashboards.

## Quick Start

### 1. Environment

```bash
conda env create -f environment.yml
conda activate ets-sc
pip install -e ".[all]"
```

### 2. Configure

Copy and edit `config.yaml`:
- Set your MQTT broker address
- Set LabJack serial numbers
- Set LakeShore 350 IP address
- Configure relay GPIO pins

### 3. Run

```bash
# GUI mode (default)
ets-slowcontrol

# Headless service (on the Pi)
ets-slowcontrol service

# With verbose logging
ets-slowcontrol -v -c config.yaml
```

### 4. IOTStack Services

Add Telegraf and Grafana to your IOTStack:

```bash
cd ~/IOTstack
docker compose -f docker-compose.yml \
               -f /path/to/deploy/docker-compose.override.yml up -d
```

Copy `deploy/telegraf.conf` to `~/IOTstack/volumes/telegraf/telegraf.conf`.

## Repository Structure

```
ETS-slowcontrol/
+-- config.yaml                 # System configuration
+-- pyproject.toml              # Python packaging
+-- environment.yml             # Conda environment
|
+-- slowcontrol/
|   +-- app.py                  # Entry point (GUI or service)
|   +-- core/
|   |   +-- config.py           # YAML -> typed dataclasses
|   |   +-- mqtt.py             # Paho-MQTT wrapper
|   |   +-- registry.py         # Plugin registration
|   |   +-- service.py          # Main orchestrator
|   +-- drivers/
|   |   +-- base.py             # Abstract SensorDriver
|   |   +-- labjack_t7.py       # LabJack T7 (TC, level, FRG)
|   |   +-- lakeshore350.py     # LakeShore 350 (Ethernet SCPI)
|   |   +-- dht_serial.py       # Arduino DHT serial bridge
|   |   +-- gpio_relay.py       # RPi GPIO relay control
|   |   +-- esp32_serial.py     # ESP32 serial JSON sensors
|   +-- controllers/
|   |   +-- base.py             # Abstract Controller
|   |   +-- autovalve.py        # LN2 fill state machine
|   |   +-- pid.py              # Generic PID controller
|   |   +-- humidity.py         # Humidity duty-cycle control
|   |   +-- interlocks.py       # Safety watchdog
|   +-- gui/
|       +-- main_window.py      # PyQt5 main window
|       +-- sensor_panel.py     # Live sensor readouts
|       +-- control_panel.py    # Relay / setpoint controls
|
+-- deploy/
|   +-- telegraf.conf           # MQTT -> InfluxDB config
|   +-- docker-compose.override.yml
+-- nodered/
|   +-- flows.json              # Thin button dashboard
+-- tests/
    +-- conftest.py
    +-- test_controllers.py
    +-- test_drivers.py
```

## MQTT Topic Reference

| Pattern | Direction | Description |
|---------|-----------|-------------|
| `ets/sensors/{driver}/{channel}` | publish | Sensor readings |
| `ets/commands/relay/{name}` | subscribe | Relay commands |
| `ets/commands/lakeshore/setpoint` | subscribe | Temperature setpoint |
| `ets/commands/autovalve/mode` | subscribe | Mode control |
| `ets/commands/humidity/setpoint` | subscribe | Humidity setpoint |
| `ets/status/{controller}` | publish | Controller state (retained) |
| `ets/status/relays/{name}` | publish | Relay state (retained) |
| `ets/status/service/heartbeat` | publish | Service uptime (retained) |
| `ets/alerts/warning/{rule}` | publish | Interlock alerts |

## Adding a New Sensor

### ESP32 over MQTT (no Python changes needed)

Have the ESP32 publish JSON to `ets/sensors/esp32_{id}/{channel}`.
Telegraf stores it automatically.  Grafana shows it.

### ESP32 or Arduino over Serial

1. Create `slowcontrol/drivers/my_sensor.py`:

```python
from slowcontrol.core.registry import register_driver
from slowcontrol.drivers.base import SensorDriver

@register_driver("my_sensor")
class MySensorDriver(SensorDriver):
    def connect(self):
        ...
    def read(self):
        return {"temperature": 22.1}
    def disconnect(self):
        ...
```

2. Add it to `slowcontrol/core/registry.py` imports.

3. Add to `config.yaml`:

```yaml
drivers:
  my_new_sensor:
    type: my_sensor
    poll_interval: 1.0
    port: /dev/ttyUSB2
```

## DAQ Integration

The ETS-pythonDAQ reads temperature from InfluxDB and sends setpoints:

- **Read:** `SlowControl.temperature_K()` queries InfluxDB (unchanged)
- **Write:** `set_setpoint(T_K)` publishes to `ets/commands/lakeshore/setpoint`

To connect, subclass `SlowControl` in ETS-pythonDAQ and override
`set_setpoint()` to publish an MQTT message instead of raising
`NotImplementedError`.

## Testing

```bash
pytest tests/ -v
```

## Debug

Monitor all MQTT traffic:

```bash
mosquitto_sub -h localhost -t "ets/#" -v
```
