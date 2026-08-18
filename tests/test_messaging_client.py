from __future__ import annotations

import asyncio
import json
from datetime import UTC

import pytest

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.client import (
    _client_identifier,
    _unexpected_disconnect_context,
    _utc_now,
    heartbeat_loop,
)


class _FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int]] = []
        self.did_publish = asyncio.Event()

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append((topic, payload, qos))
        self.did_publish.set()


def test_utc_now_is_timezone_aware() -> None:
    assert _utc_now().tzinfo is UTC


def test_module_entrypoint_exports_cli() -> None:
    from embeint_htf_station import __main__

    assert callable(__main__.main)


def test_client_identifier_uses_the_provisioned_mqtt_username() -> None:
    settings = Settings(
        org_id="org",
        station_id="0ee170d6-720a-46f8-97b7-83dd25ad6ae8",
        broker_username="station-0ee170d6720a46f897b783dd25ad6ae8",
    )

    assert _client_identifier(settings) == settings.broker_username


def test_client_identifier_uses_the_canonical_station_username_shape_as_a_fallback() -> None:
    settings = Settings(org_id="org", station_id="0ee170d6-720a-46f8-97b7-83dd25ad6ae8")

    assert _client_identifier(settings) == "station-0ee170d6720a46f897b783dd25ad6ae8"


def test_unexpected_disconnect_marks_a_possible_client_id_takeover() -> None:
    settings = Settings(
        org_id="org",
        station_id="0ee170d6-720a-46f8-97b7-83dd25ad6ae8",
        broker_username="station-0ee170d6720a46f897b783dd25ad6ae8",
    )

    context = _unexpected_disconnect_context(settings, RuntimeError("connection reset"))

    assert context == {
        "station_id": settings.station_id,
        "client_id": settings.broker_username,
        "reason": "connection reset",
        "possible_session_takeover": True,
    }


async def test_heartbeat_loop_publishes_an_empty_live_lane_snapshot() -> None:
    client = _FakeClient()
    settings = Settings(org_id="org", station_id="station")
    task = asyncio.create_task(heartbeat_loop(client, settings, interval_s=60))
    await asyncio.wait_for(client.did_publish.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _, payload, _ = client.published[0]
    assert json.loads(payload)["activeLanes"] == []
