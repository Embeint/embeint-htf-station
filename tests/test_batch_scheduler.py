from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from embeint_htf_station.config import LanePlanSettings, LaneSettings, Settings, StageDependencySettings, StageSettings
from embeint_htf_station.stages import StageContext, StageResult
from embeint_htf_station.stations.basic import BasicStation, _LaneActivity, _QueuedRun


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


class GatedStage:
    started = asyncio.Event()
    release = asyncio.Event()

    def __init__(self, settings: StageSettings) -> None: self.settings = settings
    async def run(self, _logger: object, _context: StageContext) -> StageResult:
        GatedStage.started.set()
        await GatedStage.release.wait()
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


async def test_independent_lanes_run_concurrently() -> None:
    station, publisher = make_station(), Publisher()
    left = (StageSettings(name="left flash", kind="probe"),)
    right = (StageSettings(name="right flash", kind="probe"),)

    await asyncio.gather(
        station._run_test(publisher, "DUT", str(uuid4()), stages=left, lane="left"),
        station._run_test(publisher, "DUT", str(uuid4()), stages=right, lane="right"),
    )

    assert ProbeStage.max_active == 2


async def test_shared_stage_lock_serializes_concurrent_lanes() -> None:
    station, publisher = make_station(), Publisher()
    left, right = station._plans["left"], station._plans["right"]
    await asyncio.gather(
        station._run_test(publisher, "L", str(uuid4()), stages=left.stages, lane="left"),
        station._run_test(publisher, "R", str(uuid4()), stages=right.stages, lane="right"),
    )
    assert ProbeStage.max_active == 1
    stages = [payload for topic, payload in publisher.messages if topic.endswith("/stage")]
    assert {payload["lane"] for payload in stages} == {"left", "right"}
    assert any(payload["status"] == "blocked" for payload in stages)
    results = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert {payload["lane"] for payload in results} == {"left", "right"}


async def test_dependent_lane_waits_for_the_prerequisite_stage() -> None:
    GatedStage.started = asyncio.Event()
    GatedStage.release = asyncio.Event()
    ProbeStage.completed = []
    settings = Settings(org_id=str(uuid4()), station_id=str(uuid4()), lanes=(
        LaneSettings(name="left", programmer="left"), LaneSettings(name="right", programmer="right"),
    ), plans=(
        LanePlanSettings(lane="left", stages=(StageSettings(name="left flash", kind="gate"),)),
        LanePlanSettings(lane="right", stages=(StageSettings(name="right flash", kind="probe", after=(
            StageDependencySettings(lane="left", stage="left flash", outcome="passed"),
        )),)),
    ))
    station = BasicStation(settings, stage_factories={"probe": ProbeStage, "gate": GatedStage})
    publisher = Publisher()
    results = {
        ("left", "left flash"): asyncio.get_running_loop().create_future(),
        ("right", "right flash"): asyncio.get_running_loop().create_future(),
    }

    left = asyncio.create_task(station._run_test(publisher, "DUT", str(uuid4()), stages=station._plans["left"].stages, lane="left", batch_results=results))
    await GatedStage.started.wait()
    right = asyncio.create_task(station._run_test(publisher, "DUT", str(uuid4()), stages=station._plans["right"].stages, lane="right", batch_results=results))
    while not any(
        payload["lane"] == "right" and payload["status"] == "blocked"
        for topic, payload in publisher.messages
        if topic.endswith("/stage")
    ):
        await asyncio.sleep(0)

    assert ProbeStage.completed == []
    GatedStage.release.set()
    left_result, right_result = await asyncio.gather(left, right)

    assert (left_result.outcome, right_result.outcome) == ("passed", "passed")
    assert ProbeStage.completed == ["right flash"]


async def test_heartbeat_reports_active_lanes() -> None:
    station, publisher = make_station(), Publisher()
    left_run_id, right_run_id = str(uuid4()), str(uuid4())
    await station._publish_heartbeat(
        publisher,
        "running",
        run_id=left_run_id,
        active_lanes=[
            _LaneActivity("left", left_run_id, "LEFT-DUT", "running", "flash"),
            _LaneActivity("right", right_run_id, "RIGHT-DUT", "blocked", "flash", "Waiting for resource lock shared"),
        ],
    )
    heartbeat = next(payload for topic, payload in publisher.messages if topic.endswith("/heartbeat"))
    assert heartbeat["currentRunId"] == left_run_id
    assert heartbeat["activeLanes"] == [
        {"lane": "left", "runId": left_run_id, "dutId": "LEFT-DUT", "status": "running", "currentStage": "flash", "waitingReason": None},
        {"lane": "right", "runId": right_run_id, "dutId": "RIGHT-DUT", "status": "blocked", "currentStage": "flash", "waitingReason": "Waiting for resource lock shared"},
    ]


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


async def test_non_batch_run_ignores_cross_lane_dependencies() -> None:
    station, publisher = make_station(), Publisher()
    dependent = (StageSettings(name="dependent", kind="probe", after=(
        StageDependencySettings(lane="left", stage="left flash", outcome="passed"),
    )),)

    result = await station._run_test(publisher, "R", str(uuid4()), stages=dependent, lane="right")

    assert result.outcome == "passed"
    assert ProbeStage.completed == ["dependent"]


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


async def test_abort_while_waiting_for_a_shared_lock_publishes_terminal_abort() -> None:
    station, publisher = make_station(), Publisher()
    held_lock = asyncio.Lock()
    await held_lock.acquire()
    station._stage_locks["shared"] = held_lock

    task = asyncio.create_task(
        station._run_test(publisher, "DUT", str(uuid4()), stages=station._plans["left"].stages, lane="left"),
    )
    while not any(
        payload.get("status") == "blocked"
        for topic, payload in publisher.messages
        if topic.endswith("/stage")
    ):
        await asyncio.sleep(0)

    task.cancel()
    result = await task

    assert result.outcome == "aborted"
    assert held_lock.locked()
    held_lock.release()
    terminal = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert terminal[-1]["outcome"] == "aborted"
