"""MQTT connection manager wrapping paho-mqtt."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

MessageCallback = Callable[[str, dict[str, Any]], None]


class MQTTClient:
    """Thread-safe MQTT client with JSON serialization."""

    def __init__(
        self,
        broker: str = "localhost",
        port: int = 1883,
        client_id: str = "ets-slowcontrol",
        username: str | None = None,
        password: str | None = None,
    ):
        self._broker = broker
        self._port = port
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id,
        )
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._subscriptions: dict[str, list[MessageCallback]] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────

    def connect(self) -> None:
        self._client.connect(self._broker, self._port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout=10):
            log.warning("MQTT connect timed out — will retry in background")

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self._connected.clear()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── pub / sub ──────────────────────────────────────────────

    def publish(
        self, topic: str, payload: dict[str, Any], retain: bool = False
    ) -> None:
        msg = json.dumps(payload)
        self._client.publish(topic, msg, qos=1, retain=retain)

    def subscribe(self, topic: str, callback: MessageCallback) -> None:
        with self._lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
                self._client.subscribe(topic, qos=1)
            self._subscriptions[topic].append(callback)

    def unsubscribe(self, topic: str, callback: MessageCallback) -> None:
        with self._lock:
            if topic in self._subscriptions:
                try:
                    self._subscriptions[topic].remove(callback)
                except ValueError:
                    pass
                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
                    self._client.unsubscribe(topic)

    # ── paho callbacks ─────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self._broker, self._port)
            self._connected.set()
            with self._lock:
                for topic in self._subscriptions:
                    self._client.subscribe(topic, qos=1)
        else:
            log.error("MQTT connect failed: rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc != 0:
            log.warning(
                "MQTT disconnected unexpectedly (rc=%d), will auto-reconnect", rc
            )

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("Non-JSON message on %s", msg.topic)
            return

        with self._lock:
            callbacks: list[MessageCallback] = []
            for pattern, cbs in self._subscriptions.items():
                if mqtt.topic_matches_sub(pattern, msg.topic):
                    callbacks.extend(cbs)

        for cb in callbacks:
            try:
                cb(msg.topic, payload)
            except Exception:
                log.exception("Error in MQTT callback for %s", msg.topic)
