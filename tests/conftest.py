"""Shared test fixtures."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from slowcontrol.core.mqtt import MQTTClient


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
