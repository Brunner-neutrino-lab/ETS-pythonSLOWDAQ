"""Proprioception layer — the system's centralized state registry + store.

``state.yaml`` (at the repo root) declares every state the system tracks;
``schema.py`` parses it into typed objects; ``store.py`` subscribes to MQTT,
computes per-state freshness and moving averages, evaluates derived
expressions, and republishes a consolidated snapshot on
``ets/state/snapshot``.
"""

from slowcontrol.state.schema import (
    ControlSpec,
    Freshness,
    SchemaError,
    SourceSpec,
    StateDef,
    StateSchema,
    default_schema_path,
    load_state_schema,
)

__all__ = [
    "ControlSpec",
    "Freshness",
    "SchemaError",
    "SourceSpec",
    "StateDef",
    "StateSchema",
    "default_schema_path",
    "load_state_schema",
]
