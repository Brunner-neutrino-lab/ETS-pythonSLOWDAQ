# ETS Slow Control — Setup, Testing & Verification Guide

This document is written for the person on-site who will clone this repository
onto the Raspberry Pi, bring up all services, and verify that every component
works before we commit to the system for production use.

Work through the sections in order.  Each section builds on the previous one.
Do not skip ahead — the verification steps are cumulative.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [IOTStack services](#2-iotstack-services)
3. [Clone and install](#3-clone-and-install)
4. [Edit config.yaml](#4-edit-configyaml)
5. [Verify MQTT](#5-verify-mqtt)
6. [Test 1 — Unit tests](#6-test-1--unit-tests)
7. [Test 2 — Service starts (no hardware)](#7-test-2--service-starts-no-hardware)
8. [Test 3 — Relay control](#8-test-3--relay-control)
9. [Test 4 — LabJack sensors](#9-test-4--labjack-sensors)
10. [Test 5 — LakeShore 350](#10-test-5--lakeshore-350)
11. [Test 6 — DHT humidity sensor](#11-test-6--dht-humidity-sensor)
12. [Test 7 — Telegraf → InfluxDB pipeline](#12-test-7--telegraf--influxdb-pipeline)
13. [Test 8 — Autovalve controller](#13-test-8--autovalve-controller)
14. [Test 9 — Humidity PID controller](#14-test-9--humidity-pid-controller)
15. [Test 10 — Interlock safety system](#15-test-10--interlock-safety-system)
16. [Test 11 — Node-RED dashboard](#16-test-11--node-red-dashboard)
17. [Test 12 — GUI (optional)](#17-test-12--gui-optional)
18. [Test 13 — DAQ integration](#18-test-13--daq-integration)
19. [Go-live checklist](#19-go-live-checklist)
20. [Troubleshooting](#20-troubleshooting)

---

## 1. Prerequisites

### Hardware connected to the Pi

| Device | Connection | Notes |
|--------|-----------|-------|
| LabJack T7 (serial 470019751) | USB or Ethernet | Thermocouples (K1, K2, TC1–TC5) |
| LabJack T7 (serial 470019220) | USB or Ethernet | Level sensors (RES1–RES7), FRG pressure |
| LakeShore 350 | Ethernet (TCP) | RTDs, heater control |
| Arduino (DHT11/22) | USB serial (`/dev/ttyUSB0`) | Humidity sensor |
| Relay board | GPIO pins 13, 19, 26 | LN2 valve, GN2 valve, pressure sensor |

### Software on the Pi

| Requirement | Check command | Install if missing |
|-------------|---------------|-------------------|
| Python ≥ 3.10 | `python3 --version` | `sudo apt install python3` |
| pip | `python3 -m pip --version` | `sudo apt install python3-pip` |
| IOTStack | `ls ~/IOTstack` | See [IOTStack docs](https://sensorsiot.github.io/IOTstack/) |
| Mosquitto (via IOTStack) | `docker ps \| grep mosquitto` | IOTStack menu |
| InfluxDB 2.x (via IOTStack) | `docker ps \| grep influxdb` | IOTStack menu |
| mosquitto-clients | `which mosquitto_sub` | `sudo apt install mosquitto-clients` |
| LabJack LJM library | `python3 -c "import labjack.ljm"` | [LabJack LJM install](https://labjack.com/pages/support?doc=/software-driver/installer-downloads/ljm-software-installers-t4-t7-t8-digit/) |
| RPi.GPIO | `python3 -c "import RPi.GPIO"` | `pip3 install RPi.GPIO` |
| Git | `git --version` | `sudo apt install git` |

---

## 2. IOTStack services

Make sure these containers are running before proceeding.

```bash
cd ~/IOTstack
docker compose up -d mosquitto influxdb
docker compose ps
```

You should see `mosquitto` and `influxdb` both in state `Up`.

### Add Telegraf

```bash
# Copy our Telegraf config
mkdir -p ~/IOTstack/volumes/telegraf
cp /path/to/repo/deploy/telegraf.conf ~/IOTstack/volumes/telegraf/telegraf.conf
```

Set your InfluxDB token:

```bash
# Create a .env file in ~/IOTstack or export the variable
echo "INFLUX_TOKEN=your-influxdb-token-here" >> ~/IOTstack/.env
```

Start Telegraf:

```bash
cd ~/IOTstack
docker compose -f docker-compose.yml -f /path/to/repo/deploy/docker-compose.override.yml up -d telegraf
docker logs telegraf --tail 20
```

Look for: `[agent] Config: Loaded...` and no error messages.

### Add Grafana (optional for testing, recommended for production)

```bash
docker compose -f docker-compose.yml -f /path/to/repo/deploy/docker-compose.override.yml up -d grafana
```

Open `http://<pi-ip>:3000` in a browser.  Default login: `admin` / `admin`.

---

## 3. Clone and install

```bash
cd ~
git clone <repo-url> ets-slowcontrol
cd ets-slowcontrol

# Create virtual environment (or use conda)
python3 -m venv venv
source venv/bin/activate

# Install with all optional dependencies
pip install -e ".[all]"
```

Verify the install:

```bash
python3 -c "import slowcontrol; print(slowcontrol.__version__)"
# Should print: 0.1.0
```

---

## 4. Edit config.yaml

Open `config.yaml` and update these values for your site:

```bash
nano config.yaml
```

### Fields to verify

| Section | Field | What to check |
|---------|-------|---------------|
| `mqtt.broker` | Should be `localhost` if running on the Pi | |
| `drivers.labjack_tc.serial` | Match the serial number printed on LabJack #1 | `470019751` |
| `drivers.labjack_res.serial` | Match the serial number printed on LabJack #2 | `470019220` |
| `drivers.lakeshore.host` | IP address of the LakeShore 350 on your network | `192.168.1.50` |
| `drivers.lakeshore.port` | LakeShore TCP port (default 7777) | |
| `drivers.humidity.port` | USB serial port for Arduino | `ls /dev/ttyUSB*` to find it |
| `relays.ln2_valve.pin` | BCM GPIO pin for LN2 relay | `19` |
| `relays.gn2_valve.pin` | BCM GPIO pin for GN2 relay | `13` |
| `relays.pressure_sensor.pin` | BCM GPIO pin for pressure relay | `26` |

**Do not change any controller or interlock parameters yet.**  We will test
with defaults first.

---

## 5. Verify MQTT

Open two terminals.

**Terminal 1 — Subscribe to everything:**

```bash
mosquitto_sub -h localhost -t "ets/#" -v
```

**Terminal 2 — Publish a test message:**

```bash
mosquitto_pub -h localhost -t "ets/test" -m '{"hello": "world"}'
```

**Expected result:** Terminal 1 shows:

```
ets/test {"hello": "world"}
```

If this fails, Mosquitto is not running.  Go back to Section 2.

Leave Terminal 1 open — you will use it throughout testing.

---

## 6. Test 1 — Unit tests

```bash
cd ~/ets-slowcontrol
source venv/bin/activate
pytest tests/ -v
```

**Expected result:** All 16 tests pass.  These tests do not require any
hardware — they validate the PID controller math, autovalve state machine
logic, FRG pressure conversion, config loading, and the driver base class.

If any test fails, do not proceed.  Report the output.

---

## 7. Test 2 — Service starts (no hardware)

This test verifies that the service can start, connect to MQTT, and publish
heartbeats — even if the hardware drivers fail to connect.

Create a minimal config that skips hardware:

```bash
cat > /tmp/test-minimal.yaml << 'EOF'
mqtt:
  broker: localhost
  port: 1883
  client_id: ets-test

drivers: {}
relays: {}
controllers: {}

interlocks:
  enabled: false
EOF
```

Start the service:

```bash
python3 -m slowcontrol.app service -c /tmp/test-minimal.yaml -v
```

**In your mosquitto_sub terminal, you should see within 10 seconds:**

```
ets/status/service/heartbeat {"uptime": 0.0, "drivers": [], "controllers": [], "ts": ...}
```

The heartbeat repeats every 10 seconds with increasing uptime.

**Stop the service with Ctrl+C.**

**Pass criteria:** Heartbeat messages appear.  No crash.

---

## 8. Test 3 — Relay control

This test verifies that GPIO relays can be opened and closed via MQTT.

> **WARNING:** Make sure the LN2 dewar transfer line is disconnected or the
> manual valve upstream is closed before testing relay actuation.  We will be
> toggling real valves.

### 8a. Start with relay-only config

```bash
cat > /tmp/test-relays.yaml << 'EOF'
mqtt:
  broker: localhost
  port: 1883
  client_id: ets-relay-test

drivers: {}

relays:
  ln2_valve:       {pin: 19, default: closed, active_low: true}
  pressure_sensor: {pin: 26, default: closed, active_low: true}
  gn2_valve:       {pin: 13, default: closed, active_low: true}

controllers: {}

interlocks:
  enabled: false
EOF
```

```bash
python3 -m slowcontrol.app service -c /tmp/test-relays.yaml -v
```

### 8b. Verify default state

In `mosquitto_sub` output, you should see all three relays report `closed`:

```
ets/status/relays/ln2_valve       {"state": "closed", "ts": ...}
ets/status/relays/pressure_sensor {"state": "closed", "ts": ...}
ets/status/relays/gn2_valve       {"state": "closed", "ts": ...}
```

### 8c. Open a relay

In a separate terminal:

```bash
mosquitto_pub -h localhost -t "ets/commands/relay/pressure_sensor" \
  -m '{"action": "open"}'
```

**Expected:** In `mosquitto_sub`:

```
ets/status/relays/pressure_sensor {"state": "open", "ts": ...}
```

**Physical verification:** Listen for the relay click, or measure with a
multimeter across the relay contacts.

### 8d. Close the relay

```bash
mosquitto_pub -h localhost -t "ets/commands/relay/pressure_sensor" \
  -m '{"action": "close"}'
```

**Expected:** Status returns to `"closed"`.  Relay clicks off.

### 8e. Repeat for each relay

Test each relay individually:

```bash
# LN2 valve
mosquitto_pub -h localhost -t "ets/commands/relay/ln2_valve" -m '{"action": "open"}'
# verify click, then close:
mosquitto_pub -h localhost -t "ets/commands/relay/ln2_valve" -m '{"action": "close"}'

# GN2 valve
mosquitto_pub -h localhost -t "ets/commands/relay/gn2_valve" -m '{"action": "open"}'
mosquitto_pub -h localhost -t "ets/commands/relay/gn2_valve" -m '{"action": "close"}'
```

### 8f. Verify shutdown safety

**Stop the service with Ctrl+C** while a relay is open.

**Expected:** All relays are forced closed on shutdown.  `mosquitto_sub` shows
all three relays publish `"closed"` state before the service exits.

**Pass criteria:** Every relay opens and closes on command.  All relays close
on shutdown.

---

## 9. Test 4 — LabJack sensors

### 9a. Start with LabJack drivers only

```bash
cat > /tmp/test-labjack.yaml << 'EOF'
mqtt:
  broker: localhost
  port: 1883
  client_id: ets-lj-test

drivers:
  labjack_tc:
    type: labjack_t7
    serial: "470019751"
    poll_interval: 1.0
    channels:
      K1:  {ain: 0,  sensor: thermocouple, tc_type: K}
      K2:  {ain: 2,  sensor: thermocouple, tc_type: K}
      TC1: {ain: 4,  sensor: thermocouple, tc_type: T}

  labjack_res:
    type: labjack_t7
    serial: "470019220"
    poll_interval: 1.0
    channels:
      RES1: {ain: 0, sensor: voltage}
      RES7: {ain: 6, sensor: voltage}
      FRG1: {ain: [8, 9], sensor: frg_pressure}

relays: {}
controllers: {}
interlocks:
  enabled: false
EOF
```

```bash
python3 -m slowcontrol.app service -c /tmp/test-labjack.yaml -v
```

### 9b. Verify sensor data

In `mosquitto_sub`, you should see messages appearing every ~1 second:

```
ets/sensors/labjack_tc/K1   {"value": 22.531, "ts": ...}
ets/sensors/labjack_tc/K2   {"value": 22.487, "ts": ...}
ets/sensors/labjack_tc/TC1  {"value": -196.2, "ts": ...}
ets/sensors/labjack_res/RES1 {"value": 0.324, "ts": ...}
ets/sensors/labjack_res/RES7 {"value": 0.001, "ts": ...}
ets/sensors/labjack_res/FRG1 {"value": 1.2e-05, "ts": ...}
```

### 9c. Sanity-check values

| Channel | Expected at room temp (no cryo) | Suspicious if |
|---------|--------------------------------|---------------|
| K1, K2 | ~20–25 °C | < 0 or > 50 |
| TC1–TC5 | ~20–25 °C (or cryo temps if cold) | NaN or exactly 0 |
| RES1–RES7 | Near 0 V (no liquid) | Negative or > 10 |
| FRG1 | Depends on vacuum state | NaN |

### 9d. Add remaining channels

If the above works, stop the service, edit the config to include all channels
(TC2–TC5, RES2–RES6), restart, and confirm they all publish.

**Pass criteria:** All configured LabJack channels publish readings at the
expected rate.  Values are physically reasonable.

---

## 10. Test 5 — LakeShore 350

### 10a. Verify network connectivity

```bash
ping 192.168.1.50       # or whatever IP is configured
nc -zv 192.168.1.50 7777   # test TCP port
```

Both must succeed.

### 10b. Start with LakeShore driver only

```bash
cat > /tmp/test-lakeshore.yaml << 'EOF'
mqtt:
  broker: localhost
  port: 1883
  client_id: ets-ls-test

drivers:
  lakeshore:
    type: lakeshore350
    host: 192.168.1.50
    port: 7777
    poll_interval: 1.0
    inputs: [A, B, C, D]

relays: {}
controllers: {}
interlocks:
  enabled: false
EOF
```

```bash
python3 -m slowcontrol.app service -c /tmp/test-lakeshore.yaml -v
```

### 10c. Verify RTD readings

In `mosquitto_sub`:

```
ets/sensors/lakeshore/rtd/A    {"value": 295.123, "ts": ...}
ets/sensors/lakeshore/rtd/B    {"value": 294.987, "ts": ...}
ets/sensors/lakeshore/rtd/C    {"value": 0.0, "ts": ...}
ets/sensors/lakeshore/rtd/D    {"value": 0.0, "ts": ...}
ets/sensors/lakeshore/heater/1 {"value": 0.0, "ts": ...}
ets/sensors/lakeshore/setpoint/1 {"value": 295.0, "ts": ...}
```

Inputs with no RTD connected will read 0.0 — that is expected.  Active inputs
should read reasonable temperatures in Kelvin.

### 10d. Test setpoint command

> **WARNING:** This will change the LakeShore setpoint.  Only do this if you
> understand the thermal implications.  If heater output is enabled, the
> system will begin moving to the new temperature.

```bash
mosquitto_pub -h localhost -t "ets/commands/lakeshore/setpoint" \
  -m '{"value": 300.0, "loop": 1}'
```

**Expected in service log output:**

```
LakeShore setpoint loop 1 → 300.0000 K
```

**Verification:** Check the LakeShore front panel — the setpoint for loop 1
should now read 300.0 K.

Set it back:

```bash
mosquitto_pub -h localhost -t "ets/commands/lakeshore/setpoint" \
  -m '{"value": 295.0, "loop": 1}'
```

**Pass criteria:** RTD temperatures appear via MQTT.  Setpoint command
changes the LakeShore front-panel value.

---

## 11. Test 6 — DHT humidity sensor

### 11a. Check the serial port

```bash
ls -la /dev/ttyUSB*
# You should see /dev/ttyUSB0 (or another port)

# Quick serial test:
screen /dev/ttyUSB0 9600
# You should see lines like: 45.2,22.1
# Press Ctrl+A then K to exit screen
```

### 11b. Start with DHT driver

```bash
cat > /tmp/test-dht.yaml << 'EOF'
mqtt:
  broker: localhost
  port: 1883
  client_id: ets-dht-test

drivers:
  humidity:
    type: dht_serial
    port: /dev/ttyUSB0
    baud: 9600
    poll_interval: 5.0

relays: {}
controllers: {}
interlocks:
  enabled: false
EOF
```

```bash
python3 -m slowcontrol.app service -c /tmp/test-dht.yaml -v
```

### 11c. Verify readings

In `mosquitto_sub` (every ~5 seconds):

```
ets/sensors/humidity/humidity     {"value": 45.2, "ts": ...}
ets/sensors/humidity/temperature  {"value": 22.1, "ts": ...}
```

**Pass criteria:** Humidity and temperature values appear and are reasonable
(humidity 10–90 %RH, temperature near room temp).

---

## 12. Test 7 — Telegraf → InfluxDB pipeline

This verifies that sensor data published to MQTT is automatically stored in
InfluxDB by Telegraf.

### 12a. Publish test data

With Telegraf running (from Section 2):

```bash
mosquitto_pub -h localhost -t "ets/sensors/test_driver/test_channel" \
  -m '{"value": 42.0, "ts": 1234567890}'
```

### 12b. Query InfluxDB

```bash
docker exec influxdb influx query '
from(bucket: "slowcontrol")
  |> range(start: -5m)
  |> filter(fn: (r) => r["driver"] == "test_driver")
' --org ets --token "$INFLUX_TOKEN"
```

You should see a row with value `42.0`.

### 12c. Verify real sensor data

Start the full service (or any single driver) and wait 30 seconds:

```bash
docker exec influxdb influx query '
from(bucket: "slowcontrol")
  |> range(start: -1m)
  |> limit(n: 5)
' --org ets --token "$INFLUX_TOKEN"
```

You should see multiple rows from your sensors.

**Pass criteria:** Sensor data is queryable from InfluxDB.

---

## 13. Test 8 — Autovalve controller

> **CRITICAL:** Ensure the LN2 transfer line is disconnected or the upstream
> manual valve is closed.  The autovalve will physically open the LN2 valve.

### 13a. Start with autovalve in manual mode

```bash
# Use the full config.yaml but with autovalve enabled
python3 -m slowcontrol.app service -c config.yaml -v
```

Immediately set the autovalve to manual mode so it does not actuate
unexpectedly:

```bash
mosquitto_pub -h localhost -t "ets/commands/autovalve/mode" \
  -m '{"mode": "manual"}'
```

### 13b. Verify state reporting

In `mosquitto_sub`:

```
ets/status/autovalve {"state": "WAITING", "mode": "gradient", "enabled": false, ...}
```

### 13c. Re-enable and observe

Switch to auto mode:

```bash
mosquitto_pub -h localhost -t "ets/commands/autovalve/mode" \
  -m '{"mode": "auto"}'
```

**Expected behavior:**

- State should be `WAITING` since we're at room temp and levels are low/flat
- If level sensors are not connected, gradients will be zero → no fill trigger
- The autovalve logs decisions at INFO level in the service output

### 13d. Test mode switching

```bash
# Switch to threshold mode
mosquitto_pub -h localhost -t "ets/commands/autovalve/mode" \
  -m '{"mode": "threshold"}'
```

Verify in `mosquitto_sub` that the status updates to `"mode": "threshold"`.

Switch back:

```bash
mosquitto_pub -h localhost -t "ets/commands/autovalve/mode" \
  -m '{"mode": "gradient"}'
```

### 13e. Simulate a fill (optional, advanced)

To test the fill state machine without liquid nitrogen, you can publish
simulated sensor data.  In a separate terminal, publish a declining level
gradient:

```bash
for i in $(seq 1 30); do
  val=$(echo "5.0 - 0.01 * $i" | bc)
  mosquitto_pub -h localhost -t "ets/sensors/labjack_res/RES1" \
    -m "{\"value\": $val, \"ts\": $(date +%s)}"
  sleep 1
done
```

If the gradient exceeds the trigger threshold (`-1e-4 V/s`), the autovalve
should transition: `WAITING → FILLING`.  You will see:

```
ets/status/autovalve {"state": "FILLING", ...}
ets/commands/relay/ln2_valve {"action": "open", "source": "autovalve"}
```

It will close after `fill_timeout` (600 s) or if you publish an overfill
value:

```bash
mosquitto_pub -h localhost -t "ets/sensors/labjack_res/RES7" \
  -m '{"value": 10.0, "ts": ...}'
```

**Pass criteria:** Autovalve state machine transitions are correct.  Relay
commands are published.  Mode switching works.

---

## 14. Test 9 — Humidity PID controller

### 14a. Verify the controller starts

With the full `config.yaml` service running, check `mosquitto_sub` for:

```
ets/status/humidity_pid {"setpoint": 7.2, "measured": null, "duty_cycle": 0.0, ...}
```

`measured: null` means no humidity data has arrived yet.  `duty_cycle: 0.0`
means the relay is not being driven.

### 14b. Send a humidity reading

```bash
mosquitto_pub -h localhost -t "ets/sensors/humidity/humidity" \
  -m '{"value": 50.0, "ts": ...}'
```

The PID should compute a non-zero duty cycle (since 50.0 > 7.2 setpoint) and
begin cycling the GN2 relay.

### 14c. Change the setpoint

```bash
mosquitto_pub -h localhost -t "ets/commands/humidity/setpoint" \
  -m '{"value": 10.0}'
```

Verify in `mosquitto_sub` that the status now shows `"setpoint": 10.0`.

**Pass criteria:** PID controller responds to humidity data.  Setpoint
changes via MQTT.  GN2 relay toggles during duty-cycle on-period.

---

## 15. Test 10 — Interlock safety system

### 15a. Verify interlocks are running

In `mosquitto_sub`:

```
ets/status/interlocks {"all_ok": true, "violations": {}, ...}
```

### 15b. Trigger the overfill interlock

Publish a voltage above the overfill threshold (9.8 V):

```bash
mosquitto_pub -h localhost -t "ets/sensors/labjack_res/RES7" \
  -m '{"value": 10.0, "ts": ...}'
```

**Expected:**

```
ets/alerts/warning/overfill {"message": "...", "value": 10.0, "threshold": 9.8, ...}
ets/commands/relay/ln2_valve {"action": "close", "source": "interlock"}
ets/status/interlocks {"all_ok": false, "violations": {"overfill": "..."}, ...}
```

### 15c. Clear the interlock

Publish a value below threshold:

```bash
mosquitto_pub -h localhost -t "ets/sensors/labjack_res/RES7" \
  -m '{"value": 5.0, "ts": ...}'
```

The violation should clear and `all_ok` should return to `true`.

### 15d. Test the watchdog

Stop all drivers (or just stop publishing sensor data) and wait 60 seconds.

**Expected:** The watchdog triggers and closes all relays:

```
ets/status/interlocks {"all_ok": false, "violations": {"watchdog": "No sensor data for 61s"}, ...}
ets/commands/relay/ln2_valve {"action": "close", "source": "watchdog"}
ets/commands/relay/gn2_valve {"action": "close", "source": "watchdog"}
```

Resume publishing sensor data — the watchdog should clear.

**Pass criteria:** Overfill interlock triggers relay close.  Watchdog fires
after timeout.  Both clear when conditions normalize.

---

## 16. Test 11 — Web control panel

> Node-RED was decommissioned on 2026-05-25 in favour of a Flask-based control
> panel that auto-renders from `state.yaml`.  The old `nodered/flows.json` is
> kept in the repo as historical reference but the container is no longer
> deployed.

### 16a. Install and start

```bash
pip install -r webcontrol/requirements.txt
sudo cp webcontrol/ets-webcontrol.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ets-webcontrol
systemctl status ets-webcontrol
```

### 16b. Open the pages

| URL | Page |
|-----|------|
| `http://<pi-ip>:8088/` | **Register** — every state grouped by category, with freshness pills and 60 s averages on analog states |
| `http://<pi-ip>:8088/control` | **Control** — relays, LakeShore loop 1, autovalve mode, humidity PID |
| `http://<pi-ip>:8088/readme` | Repo README rendered in-browser |

The pages poll `/api/state` every 1.5 s, which serves the consolidated state
snapshot the StateStore publishes on `ets/state/snapshot`.

### 16c. Test relay buttons

On `/control`, click **Open** under "Pressure sensor" and verify in
`mosquitto_sub`:

```
ets/commands/relay/pressure_sensor {"action":"open"}
ets/status/relays/pressure_sensor   {"state":"open", "ts": ...}
```

The actual state in the page updates within ~1.5 s. Repeat with **Close**.

LN₂ **Open** asks for a confirmation since it actuates the dewar transfer
valve.

### 16d. Test LakeShore controls

The LakeShore card spans two columns and has separate apply blocks for
setpoint, PID gains, heater range (with a confirmation prompt for range 4 or
5), output mode, manual output, and setpoint ramp. Verify each by:

1. Reading the current value in the header row of the card
2. Entering the same value in the input (the field's placeholder text shows
   the current value)
3. Clicking Apply and checking the service log shows the SCPI write

```bash
journalctl -u ets-slowcontrol -f
# expect lines like:
#   LakeShore PID out 1 → P=680.000 I=1.000 D=0.000
#   LakeShore RANGE out 1 → 0
```

### 16e. Test autovalve / humidity controls

- Autovalve: click each mode button (gradient / threshold / manual) and
  verify the active mode highlights and `ets/status/autovalve` reports the
  new mode within ~1 s.
- Humidity: enter a new setpoint and click Apply. Verify `ets/status/humidity_pid`
  reports the new setpoint immediately (the controller publishes on change).

**Pass criteria:** All buttons round-trip through MQTT to the hardware /
controller and back to the page within ~2 s.

---

## 17. Test 12 — GUI (optional)

The PyQt GUI can be run from a machine with a display (the Pi with a monitor,
or a remote machine with X forwarding / VNC).

```bash
python3 -m slowcontrol.app gui -c config.yaml
```

### Verify

- **Sensors tab:** Should populate with live sensor readings grouped by driver
- **Controls tab:**
  - Relay buttons: Click "Open" / "Close" for each relay and verify actuation
  - LakeShore setpoint: Enter a value and click "Set" — verify the front panel
  - Autovalve: Click "Auto" / "Manual" — verify mode changes in status
  - Humidity: Change setpoint — verify in MQTT
  - Interlocks: Should show "OK" in green (or "ALERT" if a condition is active)
- **Status bar:** Should show "Connected | N drivers | N controllers"

**Pass criteria:** GUI displays live data.  All controls actuate correctly.

---

## 18. Test 13 — DAQ integration

This tests that ETS-pythonDAQ can read temperatures and send setpoints through
the slow control system.

### 18a. Verify InfluxDB reads

On the DAQ machine (or the Pi):

```python
from daq.slowcontrol import SlowControl
from daq.config import ExperimentConfig

cfg = ExperimentConfig(
    influxdb_url="http://<pi-ip>:8086",
    influxdb_org="ets",
    influxdb_bucket="slowcontrol",
    influxdb_rtd_field="rtd/A",       # adjust to match your Telegraf field
    influxdb_token="your-token",
)
sc = SlowControl(cfg)
sc.connect()

T = sc.temperature_K()
print(f"Temperature: {T:.2f} K")

all_rtds = sc.all_rtds_K()
print(f"All RTDs: {all_rtds}")
```

**Expected:** Prints a reasonable temperature in Kelvin.

### 18b. Verify setpoint command (if LakeShoreSlowControl is implemented)

```python
from daq.lakeshore_slowcontrol import LakeShoreSlowControl

cfg = ExperimentConfig(
    mqtt_broker="<pi-ip>",
    mqtt_port=1883,
    # ... InfluxDB fields as above ...
)
sc = LakeShoreSlowControl(cfg)
sc.set_setpoint(295.0)
```

Verify the LakeShore front panel updates.

See `ETS-pythonDAQ/docs/slowcontrol_lakeshore_integration.md` for full
instructions on implementing the DAQ-side subclass.

**Pass criteria:** DAQ can read temperature from InfluxDB.  Setpoint
commands reach the LakeShore.

---

## 19. Go-live checklist

Once all tests pass, use this checklist before switching to the new system:

- [ ] All 16 unit tests pass
- [ ] Service starts and publishes heartbeats
- [ ] All 3 relays actuate correctly and close on shutdown
- [ ] Both LabJacks publish all channels at 1 Hz
- [ ] LakeShore 350 publishes RTD values and accepts setpoint commands
- [ ] DHT humidity sensor publishes readings
- [ ] Telegraf is writing sensor data to InfluxDB
- [ ] Autovalve state machine transitions correctly
- [ ] Humidity PID controller cycles the GN2 relay
- [ ] Overfill interlock closes LN2 valve when triggered
- [ ] Watchdog closes all valves after timeout
- [ ] Node-RED dashboard buttons work remotely
- [ ] DAQ can read temperature from InfluxDB
- [ ] DAQ can send setpoint via MQTT (if implemented)

### Deploy for production

```bash
# Start as a systemd service so it survives reboots
sudo tee /etc/systemd/system/ets-slowcontrol.service << EOF
[Unit]
Description=ETS Slow Control Service
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ets-slowcontrol
ExecStart=/home/pi/ets-slowcontrol/venv/bin/python -m slowcontrol.app service -c /home/pi/ets-slowcontrol/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ets-slowcontrol
sudo systemctl start ets-slowcontrol

# Check status
sudo systemctl status ets-slowcontrol
journalctl -u ets-slowcontrol -f
```

---

## 20. Troubleshooting

### Service won't start

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: labjack.ljm` | LJM not installed | Install [LabJack LJM](https://labjack.com/pages/support) |
| `ModuleNotFoundError: RPi.GPIO` | Not on a Pi, or missing package | `pip install RPi.GPIO` |
| `MQTT connect timed out` | Mosquitto not running | `docker compose up -d mosquitto` |
| `Connection refused` on LakeShore | Wrong IP/port or firewall | `nc -zv <ip> <port>` to test |
| `LabJack T7 ... not found` | Wrong serial or not connected | Check USB cable, run `ljm_list_all` |

### No data in InfluxDB

1. Check Telegraf logs: `docker logs telegraf --tail 50`
2. Check Telegraf is subscribed: publish a test message and look for it in logs
3. Verify the InfluxDB bucket "slowcontrol" exists
4. Verify the INFLUX_TOKEN is correct

### Relay doesn't click

1. Check GPIO pin with `gpio readall` (wiringPi) or `raspi-gpio get <pin>`
2. Verify `active_low` matches your relay board (most boards are active-low)
3. Test GPIO directly: `raspi-gpio set <pin> op; raspi-gpio set <pin> dl`

### Autovalve not triggering

1. Check that sensor data is actually reaching the autovalve:
   `mosquitto_sub -t "ets/sensors/labjack_res/#" -v`
2. Verify mode is `auto` (not `manual`):
   `mosquitto_sub -t "ets/status/autovalve" -v`
3. If using gradient mode, there may not be enough data yet (needs ~60 samples)
4. Switch to threshold mode for testing:
   `mosquitto_pub -t "ets/commands/autovalve/mode" -m '{"mode": "threshold"}'`

### LakeShore not responding

1. Verify TCP connectivity: `nc -zv <ip> <port>`
2. Try a raw SCPI query: `echo "*IDN?" | nc <ip> <port>`
3. Check if another process is holding the TCP connection (only one client at a time)
4. Power-cycle the LakeShore Ethernet module if needed
