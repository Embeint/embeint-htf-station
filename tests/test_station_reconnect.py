import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiomqtt
import pytest

from embeint_htf_station.config import Settings
from embeint_htf_station.contracts.mqtt import Command
from embeint_htf_station.messaging import renewal
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
    async def connect(settings, *, inbox):
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


async def test_certificate_handover_keeps_aiomqtt_inflight_buffered_and_teardown_commands(tmp_path, monkeypatch):
    station = basic.BasicStation(Settings(
        _env_file=None, org_id="org-1", station_id="station-1",
        firmware_cache_dir=str(tmp_path / "firmware"),
    ))
    commands = [Command(id=uuid4(), kind="run-plan", payload={
        "runId": str(uuid4()), "dutId": f"DUT-{index}", "lane": "default",
    }) for index in range(3)]
    messages = [aiomqtt.Message("commands", command.model_dump_json().encode(), 1, False, index, None)
                for index, command in enumerate(commands)]
    executed = []
    clients = []
    checks = 0
    consumed = asyncio.Event()
    original_to_thread = asyncio.to_thread

    class Reconnected(Exception):
        pass

    async def run_test(client, dut_id, run_id, **kwargs):
        executed.append(run_id)
        await asyncio.sleep(0)
        return SimpleNamespace(outcome="pass")

    @asynccontextmanager
    async def connect(settings, *, inbox):
        if len(clients) == 2:
            raise Reconnected

        class ObservedQueue(inbox.queue_type):
            async def get(self):
                message = await super().get()
                consumed.set()
                return message

        # Use aiomqtt's real MessagesIterator, including its inner queue task.
        client = aiomqtt.Client("localhost", queue_type=ObservedQueue)
        client.subscribe = AsyncMock()
        client.publish = AsyncMock()
        clients.append(client)
        try:
            yield client
        finally:
            if len(clients) == 1:
                assert consumed.is_set(), "test must consume the inner queue before cancelling the reader"
                assert executed == [], "the outer reader must still be unfinished at handover"
                # Delivery can also happen while the old connection is closing.
                client._queue.put_nowait(messages[2])

    async def to_thread(function, *args, **kwargs):
        nonlocal checks
        if function != station._certificate_renewer.check:
            return await original_to_thread(function, *args, **kwargs)
        checks += 1
        if checks == 2:
            finished = asyncio.get_running_loop().create_future()

            def arrive_during_renewal():
                clients[0]._queue.put_nowait(messages[0])
                clients[0]._queue.put_nowait(messages[1])
                # The queue getter runs first; the station resumes before
                # aiomqtt's outer iterator gets its completion callback.
                finished.set_result(True)

            asyncio.get_running_loop().call_soon(arrive_during_renewal)
            return await finished
        return len(executed) == len(commands)

    async def heartbeat(*args):
        await asyncio.Event().wait()

    def fast_messages(*args, **kwargs):
        return renewal.renewing_messages(*args, **kwargs, poll_seconds=.001)

    monkeypatch.setattr(basic, "connect", connect)
    monkeypatch.setattr(basic, "renewing_messages", fast_messages)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())
    monkeypatch.setattr(station, "_run_test", run_test)
    monkeypatch.setattr(station, "_serve_heartbeat_loop", heartbeat)
    with pytest.raises(Reconnected):
        await asyncio.wait_for(station.serve_forever(), timeout=5)
    assert executed == [command.payload["runId"] for command in commands]
    assert not station._command_receipts.incomplete()
    for command in commands:
        assert not station._command_receipts.claim(command), "command receipt must survive handover"
