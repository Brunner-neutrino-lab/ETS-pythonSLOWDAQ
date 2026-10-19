"""Shared test fixtures."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from slowcontrol.core.mqtt import MQTTClient


@pytest.fixture(autouse=True)
def isolated_runtime_dir(tmp_path, monkeypatch):
    """Point persisted controller state at a per-test temp dir.

    Controllers read/write runtime/*.yaml relative to the CWD, so running the
    suite from the deployed checkout would otherwise load the live service's
    tunables (e.g. fill_timeout/cooldown) and could overwrite them.
    """
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("ETS_RUNTIME_DIR", str(runtime_dir))
    return runtime_dir


@pytest.fixture
def mock_mqtt():
    """A MQTTClient mock that records publishes and subscriptions."""
    client = MagicMock(spec=MQTTClient)
    client.is_connected = True
    client._published: list[tuple[str, dict]] = []

    def _pub(topic, payload, retain=False):
        client._published.append((topic, payload))

    client.publish.side_effect = _pub
    return client
