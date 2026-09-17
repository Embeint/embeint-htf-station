import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiomqtt
import pytest

from embeint_htf_station.config import Settings
from embeint_htf_station.contracts.mqtt import Command
from embeint_htf_station.stations import basic
from embeint_htf_station.stations.scheduler import LaneScheduler


@pytest.mark.parametrize("heartbeat_fails", [True, False])
async def test_reconnect_stops_previous_scheduler_and_active_run(tmp_path, monkeypatch, heartbeat_fails):
    station = basic.BasicStation(Settings(
        _env_file=None, org_id="org-1", station_id="station-1",
        firmware_cache_dir=str(tmp_path / "firmware"),
    ))
    run_started = asyncio.Event()
    run_stopped = asyncio.Event()
    heartbeat_ready = asyncio.Event()
    schedulers = []
    command = Command(id=uuid4(), kind="run-plan", payload={
        "runId": str(uuid4()), "dutId": "DUT-001", "lane": "default",
    })

    class ObservedScheduler(LaneScheduler):
        def start(self):
            super().start()
            schedulers.append(self)

    class Reconnected(Exception):
        pass

    async def run_test(*args, **kwargs):
        run_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            run_stopped.set()

    async def heartbeat(*args):
        await run_started.wait()
        heartbeat_ready.set()
        if heartbeat_fails:
            raise aiomqtt.MqttError("heartbeat connection lost")
        await asyncio.Event().wait()

    async def messages():
        yield SimpleNamespace(payload=command.model_dump_json().encode())
        await heartbeat_ready.wait()
        raise aiomqtt.MqttError("message connection lost")

    @asynccontextmanager
    async def connect(settings):
        if schedulers:
            assert run_stopped.is_set(), "previous hardware run survived reconnect"
            assert all(worker.done() for worker in schedulers[0]._workers.values())
            assert not schedulers[0].active_runs
            raise Reconnected
        yield SimpleNamespace(messages=messages(), subscribe=AsyncMock(), publish=AsyncMock())

    monkeypatch.setattr(basic, "LaneScheduler", ObservedScheduler)
    monkeypatch.setattr(basic, "connect", connect)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())
    monkeypatch.setattr(station, "_run_test", run_test)
    monkeypatch.setattr(station, "_serve_heartbeat_loop", heartbeat)
    monkeypatch.setattr(station, "_publish_aborted_without_stages", AsyncMock())
    try:
        with pytest.raises(Reconnected):
            await asyncio.wait_for(station.serve_forever(), timeout=10)
        assert len(schedulers) == 1
    finally:
        # Also clean up if a regression leaves the first session running.
        for scheduler in schedulers:
            await scheduler.stop()
