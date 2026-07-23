from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from embeint_htf_station.config import LanePlanSettings, LaneSettings, Settings, StageDependencySettings, StageSettings
from embeint_htf_station.stages import StageContext, StageResult
from embeint_htf_station.stations.basic import BasicStation, _QueuedRun


class Publisher:
    def __init__(self) -> None: self.messages: list[tuple[str, dict[str, object]]] = []
    async def publish(self, topic: str, payload: str, qos: int = 0) -> None: self.messages.append((topic, json.loads(payload)))


class ProbeStage:
    active = 0
    max_active = 0
    completed: list[str] = []

    def __init__(self, settings: StageSettings) -> None: self.settings = settings
    async def run(self, _logger: object, _context: StageContext) -> StageResult:
        ProbeStage.active += 1
        ProbeStage.max_active = max(ProbeStage.max_active, ProbeStage.active)
        await asyncio.sleep(0.01)
        ProbeStage.active -= 1
        ProbeStage.completed.append(self.settings.name)
        now = datetime.now(UTC)
        return StageResult(self.settings.name, "passed", now, now)


def make_station() -> BasicStation:
    ProbeStage.active = ProbeStage.max_active = 0
    ProbeStage.completed = []
    return BasicStation(Settings(org_id=str(uuid4()), station_id=str(uuid4()), lanes=(
        LaneSettings(name="left", programmer="left"), LaneSettings(name="right", programmer="right"),
    ), plans=(
        LanePlanSettings(lane="left", stages=(StageSettings(name="left flash", kind="probe", locks=("shared",)),)),
        LanePlanSettings(lane="right", stages=(StageSettings(name="right flash", kind="probe", locks=("shared",)),)),
    )), stage_factories={"probe": ProbeStage})


async def test_shared_stage_lock_serializes_concurrent_lanes() -> None:
    station, publisher = make_station(), Publisher()
    left, right = station._plans["left"], station._plans["right"]
    await asyncio.gather(
        station._run_test(publisher, "L", str(uuid4()), stages=left.stages, lane="left"),
        station._run_test(publisher, "R", str(uuid4()), stages=right.stages, lane="right"),
    )
    assert ProbeStage.max_active == 1


async def test_dependency_failure_finishes_only_dependent_lane_and_aborts_remainder() -> None:
    station, publisher = make_station(), Publisher()
    dependent = (StageSettings(name="dependent", kind="probe", after=(
        StageDependencySettings(lane="left", stage="left flash", outcome="failed"),
    )), StageSettings(name="never", kind="probe"))
    future = asyncio.get_running_loop().create_future()
    future.set_result("passed")
    result = await station._run_test(publisher, "R", str(uuid4()), stages=dependent, lane="right", batch_results={
        ("left", "left flash"): future,
    })
    assert result.outcome == "failed"
    assert ProbeStage.completed == []
    assert any(message[1].get("status") == "aborted" for message in publisher.messages if message[0].endswith("/stage"))


async def test_invalid_batch_entry_publishes_terminal_error_result() -> None:
    station, publisher = make_station(), Publisher()
    queues = {lane: asyncio.Queue() for lane in station._plans}
    run_id = str(uuid4())
    await station._enqueue_batch(publisher, {"runs": [{"runId": run_id, "lane": "missing", "dutId": "DUT"}]}, queues)
    result = next(payload for topic, payload in publisher.messages if topic.endswith("/result"))
    assert result["runId"] == run_id
    assert result["outcome"] == "error"


async def test_abort_removes_queued_run_and_publishes_terminal_abort() -> None:
    station, publisher = make_station(), Publisher()
    queues = {lane: asyncio.Queue() for lane in station._plans}
    run_id = str(uuid4())
    await queues["left"].put(_QueuedRun(run_id, "DUT", station._plans["left"], {}))
    await station._abort_queued_run(publisher, queues, run_id)
    assert queues["left"].empty()
    result = next(payload for topic, payload in publisher.messages if topic.endswith("/result"))
    assert result["outcome"] == "aborted"
