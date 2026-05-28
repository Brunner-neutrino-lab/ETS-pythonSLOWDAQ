# ETS Slow Control

A unified Python-based slow control system for the ETS cryostat, replacing the
previous collection of independent scripts with a single, MQTT-centric service.

## Architecture

```
ESP32 (WiFi/MQTT) -----+
                        |
LabJack T7 (x2) ---+   |       +-----------------------------------+
LakeShore 350  ----|   +-------|  Python Slow Control Service      |
Arduino DHT    ----|   |       |                                   |
RPi GPIO relays ---+   |       |  Drivers   -> MQTT publish        |--> Relays
                        |       |  MQTT sub  -> Controllers (PID)   |
                        |       |  Interlocks -> safety watchdog    |
                        |       |  StateStore -> consolidated snap  |
                        +-------+-----------------------------------+
                                        |
                                   Mosquitto (MQTT)
                                   +----+----+
                              Telegraf   ETS web control
                                 |       (Flask, port 8088)
                             InfluxDB 2.x
                              +--+--+
                          Grafana  ETS-pythonDAQ
```

**Key principle:** MQTT is the universal message bus. Python owns all logic.
The web control panel (`webcontrol/`) provides browser-accessible read-out
and operator controls. Grafana provides historical dashboards.

The `StateStore` reads `state.yaml`, subscribes to every source MQTT topic,
computes freshness + moving averages, and republishes a consolidated
snapshot on `ets/state/snapshot` (retained, 1 Hz). The web UI auto-renders
from that snapshot — add a sensor to `state.yaml` and it appears.

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

### 5. Web control panel

Install + enable the browser UI (Flask + paho-mqtt):

```bash
pip install -r webcontrol/requirements.txt
sudo cp webcontrol/ets-webcontrol.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ets-webcontrol
```

Then open `http://<pi>:8088/`:
- `/` — **register** page: every state, grouped, with freshness pills
- `/control` — relay buttons, LakeShore loop-1 control (setpoint, PID, range,
  manual output, output mode, ramp), autovalve mode, humidity PID setpoint
- `/readme` — this file, rendered in-browser

### 6. Grafana dashboard (optional)

Import `deploy/grafana/ets-dashboard.json` via Grafana → Dashboards → Import.
Pick your InfluxDB datasource when prompted. The dashboard expects the
datasource to be in **Flux** mode (not InfluxQL).

### 7. Auth (recommended)

Credentials live in **`/etc/ets-slowcontrol/secrets.env`** (`chmod 640
root:pi`) and are loaded by both systemd units via `EnvironmentFile=`:

```
ETS_WEB_USER=ets        # basic auth for /control + /api/cmd
ETS_WEB_PASS=...        # (read-only pages stay open)
ETS_MQTT_USER=ets       # used by the slow-control service + webcontrol
ETS_MQTT_PASS=...       # to authenticate to mosquitto
```

For Mosquitto, generate the password file once:

```bash
sudo docker exec mosquitto mosquitto_passwd -c -b /mosquitto/pwfile/pwfile ets <password>
```

and enable in `mosquitto.conf`:

```
password_file /mosquitto/pwfile/pwfile
allow_anonymous false
```

Telegraf reads the same credentials from the IOTstack `.env` (`MQTT_USER`,
`MQTT_PASS`) and passes them in through the `[[inputs.mqtt_consumer]]`
block (see `deploy/telegraf.conf`).

## Repository Structure

```
ETS-slowcontrol/
+-- config.yaml                 # Drivers / relays / controllers / interlocks
+-- state.yaml                  # State registry consumed by StateStore + web UI
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
|   +-- state/                  # Proprioception layer
|   |   +-- schema.py           # state.yaml -> StateDef objects
|   |   +-- store.py            # StateStore: snapshot publisher (1 Hz)
|   +-- drivers/
|   |   +-- base.py             # Abstract SensorDriver
|   |   +-- labjack_t7.py       # LabJack T7 (TC, level, FRG)
|   |   +-- lakeshore350.py     # LakeShore 350 (RTDs + loop-1 PID controls)
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
|       +-- main_window.py      # PyQt5 main window (optional, local-only)
|       +-- sensor_panel.py     # Live sensor readouts
|       +-- control_panel.py    # Relay / setpoint controls
|
+-- webcontrol/                 # Browser UI (replaces Node-RED)
|   +-- app.py                  # Flask + paho-mqtt bridge on :8088
|   +-- templates/
|   |   +-- index.html          # /        — register (read-only grid)
|   |   +-- control.html        # /control — relays, LakeShore, autovalve, humidity
|   |   +-- readme.html         # /readme  — this file
|   +-- ets-webcontrol.service  # systemd unit
|   +-- requirements.txt
|
+-- deploy/
|   +-- telegraf.conf           # MQTT -> InfluxDB config
|   +-- docker-compose.override.yml
|   +-- grafana/
|       +-- ets-dashboard.json  # Grafana dashboard (import via UI)
|
+-- nodered/                    # Decommissioned 2026-05-25; kept for reference
|   +-- flows.json
+-- tests/
    +-- conftest.py
    +-- test_controllers.py
    +-- test_drivers.py
```

## MQTT Topic Reference

| Pattern | Direction | Description |
|---------|-----------|-------------|
| `ets/sensors/{driver}/{channel}` | publish | Sensor readings |
| `ets/sensors/lakeshore/{rtd\|heater\|setpoint\|pid_p\|pid_i\|pid_d\|range\|mout\|outmode\|ramp_on\|ramp_rate\|rampst}/{n}` | publish | LakeShore loop readouts |
| `ets/commands/relay/{name}` | subscribe | Relay commands (`{"action":"open"\|"close"}`) |
| `ets/commands/lakeshore/setpoint` | subscribe | Loop setpoint (`{"value":K,"loop":1}`) |
| `ets/commands/lakeshore/pid` | subscribe | PID gains (`{"output":1,"p":..,"i":..,"d":..}`) |
| `ets/commands/lakeshore/range` | subscribe | Heater range (`{"output":1,"range":0..5}`) |
| `ets/commands/lakeshore/mout` | subscribe | Manual heater output % |
| `ets/commands/lakeshore/outmode` | subscribe | Output mode (0..5) |
| `ets/commands/lakeshore/ramp` | subscribe | Setpoint ramp on/off + rate K/min |
| `ets/commands/autovalve/mode` | subscribe | `gradient` / `threshold` / `manual` |
| `ets/commands/humidity/setpoint` | subscribe | Humidity PID setpoint |
| `ets/status/{controller}` | publish | Controller state (retained) |
| `ets/status/relays/{name}` | publish | Relay state (retained) |
| `ets/status/service/heartbeat` | publish | Service uptime (retained) |
| `ets/state/snapshot` | publish | Consolidated state registry (retained, 1 Hz) |
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
