"""Configuration management — loads YAML config into typed dataclasses."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MQTTConfig:
    broker: str = "localhost"
    port: int = 1883
    client_id: str = "ets-slowcontrol"


@dataclass
class DriverConfig:
    type: str
    poll_interval: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> DriverConfig:
        d = dict(d)  # copy so pop is safe
        dtype = d.pop("type")
        poll = d.pop("poll_interval", 1.0)
        return cls(type=dtype, poll_interval=poll, params=d)


@dataclass
class RelayConfig:
    pin: int
    default: str = "closed"
    active_low: bool = True


@dataclass
class ControllerConfig:
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> ControllerConfig:
        d = dict(d)
        enabled = d.pop("enabled", True)
        return cls(enabled=enabled, params=d)


@dataclass
class InterlockRule:
    topic: str
    condition: str  # ">", "<", ">=", "<="
    threshold: float
    action: str
    relay: str


@dataclass
class InterlocksConfig:
    enabled: bool = True
    watchdog_timeout: float = 60.0
    rules: dict[str, InterlockRule] = field(default_factory=dict)


@dataclass
class AppConfig:
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    drivers: dict[str, DriverConfig] = field(default_factory=dict)
    relays: dict[str, RelayConfig] = field(default_factory=dict)
    controllers: dict[str, ControllerConfig] = field(default_factory=dict)
    interlocks: InterlocksConfig = field(default_factory=InterlocksConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls._parse(raw)

    @classmethod
    def _parse(cls, raw: dict) -> AppConfig:
        cfg = cls()

        if "mqtt" in raw:
            cfg.mqtt = MQTTConfig(**raw["mqtt"])

        for name, d in raw.get("drivers", {}).items():
            cfg.drivers[name] = DriverConfig.from_dict(d)

        for name, r in raw.get("relays", {}).items():
            cfg.relays[name] = RelayConfig(**r)

        for name, c in raw.get("controllers", {}).items():
            cfg.controllers[name] = ControllerConfig.from_dict(c)

        if "interlocks" in raw:
            il = raw["interlocks"]
            rules = {}
            for rname, r in il.get("rules", {}).items():
                rules[rname] = InterlockRule(**r)
            cfg.interlocks = InterlocksConfig(
                enabled=il.get("enabled", True),
                watchdog_timeout=il.get("watchdog_timeout", 60.0),
                rules=rules,
            )

        return cfg
