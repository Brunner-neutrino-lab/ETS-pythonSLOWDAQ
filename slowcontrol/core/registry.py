"""Plugin registry for drivers and controllers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slowcontrol.drivers.base import SensorDriver
    from slowcontrol.controllers.base import Controller

DRIVER_REGISTRY: dict[str, type[SensorDriver]] = {}
CONTROLLER_REGISTRY: dict[str, type[Controller]] = {}


def register_driver(type_name: str):
    """Decorator: register a driver class under its config ``type`` name."""
    def decorator(cls):
        DRIVER_REGISTRY[type_name] = cls
        return cls
    return decorator


def register_controller(name: str):
    """Decorator: register a controller class under its config key."""
    def decorator(cls):
        CONTROLLER_REGISTRY[name] = cls
        return cls
    return decorator


def load_plugins() -> None:
    """Import all driver and controller modules to trigger registration."""
    from slowcontrol.drivers import (  # noqa: F401
        labjack_t7,
        lakeshore350,
        dht_serial,
        gpio_relay,
        esp32_serial,
    )
    from slowcontrol.controllers import (  # noqa: F401
        autovalve,
        humidity,
        interlocks,
    )
