from __future__ import annotations

import asyncio
import json
from datetime import UTC

import pytest

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.client import _utc_now, heartbeat_loop


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
