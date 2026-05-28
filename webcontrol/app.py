#!/usr/bin/env python3
"""ETS web control panel.

A small Flask app that bridges the MQTT slow-control bus to a browser UI:

* It subscribes to the consolidated state snapshot the slow-control service
  publishes on  ets/state/snapshot  and serves it at /api/state — the page
  renders both its read-out and its control widgets from that (the snapshot
  carries each state's kind, unit, freshness, moving averages and, where
  applicable, its command topic/payload).
* It also keeps the raw ets/status/# and ets/sensors/# cache, for the few
  bespoke cards that need detail not in the registry.
* It publishes ets/commands/# messages on behalf of the page.

Run:
    python webcontrol/app.py            # serves on 0.0.0.0:8088
Environment overrides:
    ETS_MQTT_HOST (default localhost)   ETS_MQTT_PORT (1883)
    ETS_WEB_HOST  (default 0.0.0.0)     ETS_WEB_PORT  (8088)
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time

import markdown as _md
import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, render_template, request

MQTT_HOST = os.environ.get("ETS_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("ETS_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("ETS_MQTT_USER", "")
MQTT_PASS = os.environ.get("ETS_MQTT_PASS", "")
WEB_HOST  = os.environ.get("ETS_WEB_HOST", "0.0.0.0")
WEB_PORT  = int(os.environ.get("ETS_WEB_PORT", "8088"))
WEB_USER  = os.environ.get("ETS_WEB_USER", "")
WEB_PASS  = os.environ.get("ETS_WEB_PASS", "")

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_here, "templates"))
README_PATH = os.path.normpath(os.path.join(_here, "..", "README.md"))

# ---------------------------------------------------------------------------
# MQTT state cache
# ---------------------------------------------------------------------------
_state: dict = {}                 # topic -> {"payload": <decoded>, "ts": <epoch>}
_snapshot: dict = {}              # latest ets/state/snapshot payload
_snapshot_ts: float = 0.0         # epoch when it was received
_state_lock = threading.Lock()

SNAPSHOT_TOPIC = "ets/state/snapshot"

_client = mqtt.Client(client_id="ets-webcontrol", protocol=mqtt.MQTTv5,
                      callback_api_version=mqtt.CallbackAPIVersion.VERSION2) \
    if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client(client_id="ets-webcontrol",
                                                            protocol=mqtt.MQTTv5)
if MQTT_USER:
    _client.username_pw_set(MQTT_USER, MQTT_PASS)


def _on_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe("ets/status/#", qos=1)
    client.subscribe("ets/sensors/#", qos=1)
    client.subscribe(SNAPSHOT_TOPIC, qos=1)


def _on_message(client, userdata, msg):
    global _snapshot, _snapshot_ts
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = msg.payload.decode("utf-8", "replace")
    if msg.topic == SNAPSHOT_TOPIC:
        if isinstance(payload, dict):
            with _state_lock:
                _snapshot = payload
                _snapshot_ts = time.time()
        return
    with _state_lock:
        _state[msg.topic] = {"payload": payload, "ts": time.time()}


_client.on_connect = _on_connect
_client.on_message = _on_message


def _publish(topic: str, payload) -> None:
    if not isinstance(payload, (str, bytes)):
        payload = json.dumps(payload)
    _client.publish(topic, payload, qos=1)


# ---------------------------------------------------------------------------
# Basic auth — gates /control and /api/cmd only.  Read-only pages stay open.
# Credentials come from the ETS_WEB_USER / ETS_WEB_PASS env vars (set via
# systemd EnvironmentFile=/etc/ets-slowcontrol/secrets.env).  If either is
# empty, auth is disabled (open mode — useful for dev / first-run).
# ---------------------------------------------------------------------------
def _auth_required():
    if not WEB_USER or not WEB_PASS:
        return None
    a = request.authorization
    if a and a.username == WEB_USER and a.password == WEB_PASS:
        return None
    return Response("Authentication required.\n", 401,
                    {"WWW-Authenticate": 'Basic realm="ETS control"'})


def auth(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        deny = _auth_required()
        return deny if deny else view(*a, **kw)
    return wrapped


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # The "register GUI": a centralized read-out of every state.
    return render_template("index.html")


@app.route("/control")
@auth
def control():
    # The "control GUI": relays, LakeShore setpoint, autovalve, humidity PID.
    return render_template("control.html")


@app.route("/readme")
def readme():
    # Renders the repo-root README.md so operators can read it in the browser.
    try:
        with open(README_PATH) as fh:
            src = fh.read()
    except OSError as exc:
        return (f"<h1>README not found</h1><pre>{exc}</pre>", 404)
    body = _md.markdown(src, extensions=["fenced_code", "tables", "toc", "sane_lists"])
    return render_template("readme.html", body=body)


@app.route("/api/state")
def api_state():
    with _state_lock:
        raw = {t: v for t, v in _state.items()}
        snapshot = dict(_snapshot)
        snap_ts = _snapshot_ts
    return jsonify({
        "now": time.time(),
        "snapshot": snapshot,
        "snapshot_age": (time.time() - snap_ts) if snap_ts else None,
        "state": raw,
        "mqtt_connected": _client.is_connected(),
    })


@app.route("/api/cmd", methods=["POST"])
@auth
def api_cmd():
    d = request.get_json(force=True, silent=True) or {}
    topic = str(d.get("topic", ""))
    if not topic.startswith("ets/commands/"):
        return jsonify({"error": "topic must be under ets/commands/"}), 400
    _publish(topic, d.get("payload", {}))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
def main():
    _client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    _client.loop_start()
    try:
        from waitress import serve
        serve(app, host=WEB_HOST, port=WEB_PORT, threads=8)
    except ImportError:
        app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
