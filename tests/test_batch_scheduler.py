from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from embeint_htf_station.config import LanePlanSettings, LaneSettings, Settings, StageDependencySettings, StageSettings
from embeint_htf_station.stages import StageContext, StageResult
from embeint_htf_station.stations.basic import BasicStation, _LaneActivity, _QueuedRun, _RunPlan


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


class ErrorStage:
    def __init__(self, settings: StageSettings) -> None: self.settings = settings
    async def run(self, _logger: object, _context: StageContext) -> StageResult:
        raise RuntimeError("programmer disconnected")


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


async def test_batch_revision_mismatch_rejects_every_run_without_queueing() -> None:
    station, publisher = make_station(), Publisher()
    station._runtime_config_revision = 7
    queues = {lane: asyncio.Queue() for lane in station._plans}
    run_ids = [str(uuid4()), str(uuid4())]

    await station._enqueue_batch(publisher, {
        "configRevision": 6,
        "runs": [
            {"runId": run_ids[0], "lane": "left", "dutId": "LEFT"},
            {"runId": run_ids[1], "lane": "right", "dutId": "RIGHT"},
        ],
    }, queues)

    assert all(queue.empty() for queue in queues.values())
    results = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert {result["runId"] for result in results} == set(run_ids)
    assert {result["outcome"] for result in results} == {"error"}
    logs = [payload for topic, payload in publisher.messages if topic.endswith("/log")]
    assert any("revision mismatch" in entry["msg"] for batch in logs for entry in batch["entries"])


@pytest.mark.parametrize("aborted_index", [0, 1, 2])
async def test_abort_removes_first_middle_or_last_queued_run_without_reordering_others(aborted_index: int) -> None:
    station, publisher = make_station(), Publisher()
    queues = {lane: asyncio.Queue() for lane in station._plans}
    run_ids = [str(uuid4()) for _ in range(3)]
    for run_id in run_ids:
        await queues["left"].put(_QueuedRun(run_id, "DUT", station._plans["left"], {}))

    await station._abort_queued_run(publisher, queues, run_ids[aborted_index])

    retained = [queues["left"].get_nowait().run_id for _ in range(2)]
    assert retained == [run_id for index, run_id in enumerate(run_ids) if index != aborted_index]
    result = next(payload for topic, payload in publisher.messages if topic.endswith("/result"))
    assert result["runId"] == run_ids[aborted_index]
    assert result["outcome"] == "aborted"


async def test_abort_queued_prerequisite_resolves_its_stage_futures() -> None:
    station, publisher = make_station(), Publisher()
    run_id = str(uuid4())
    queue: asyncio.Queue[_QueuedRun] = asyncio.Queue()
    stage_future = asyncio.get_running_loop().create_future()
    run_future = asyncio.get_running_loop().create_future()
    queued = _QueuedRun(
        run_id,
        "DUT",
        station._plans["left"],
        {("left", "left flash"): stage_future, ("left", "__run__"): run_future},
    )
    await queue.put(queued)

    await station._abort_queued_run(publisher, {"left": queue}, run_id)

    assert stage_future.result() == "aborted"
    assert run_future.result() == "aborted"


async def test_cancelling_dependency_wait_preserves_future_for_other_consumers() -> None:
    station, publisher = make_station(), Publisher()
    cancelled_run_id, survivor_run_id = str(uuid4()), str(uuid4())
    dependency = asyncio.get_running_loop().create_future()
    dependent_stages = (StageSettings(name="dependent", kind="probe", after=(
        StageDependencySettings(lane="left", stage="left flash", outcome="passed"),
    )),)
    batch_results = {("left", "left flash"): dependency}
    cancelled = asyncio.create_task(station._run_test(
        publisher,
        "DUT-1",
        cancelled_run_id,
        stages=dependent_stages,
        lane="right",
        batch_results=batch_results,
    ))
    survivor = asyncio.create_task(station._run_test(
        publisher,
        "DUT-2",
        survivor_run_id,
        stages=dependent_stages,
        lane="right",
        batch_results=batch_results,
    ))
    while sum(
        payload.get("status") == "blocked"
        for topic, payload in publisher.messages
        if topic.endswith("/stage")
    ) < 2:
        await asyncio.sleep(0)

    cancelled.cancel()
    cancelled_result = await cancelled
    assert cancelled_result.outcome == "aborted"
    assert not dependency.cancelled()

    dependency.set_result("passed")
    survivor_result = await asyncio.wait_for(survivor, timeout=1)
    assert survivor_result.outcome == "passed"


async def test_lane_worker_executes_next_run_after_cancelling_dependency_wait() -> None:
    station, publisher = make_station(), Publisher()
    waiting_run_id, next_run_id = str(uuid4()), str(uuid4())
    dependency = asyncio.get_running_loop().create_future()
    waiting_plan = _RunPlan("left", (StageSettings(name="waiting", kind="probe", after=(
        StageDependencySettings(lane="right", stage="right flash", outcome="passed"),
    )),))
    next_plan = _RunPlan("left", (StageSettings(name="next", kind="probe"),))
    queue: asyncio.Queue[_QueuedRun] = asyncio.Queue()
    active_runs = {}
    active_lanes = {}
    worker = asyncio.create_task(station._lane_worker(publisher, "left", queue, active_runs, active_lanes))
    await queue.put(_QueuedRun(waiting_run_id, "DUT-1", waiting_plan, {("right", "right flash"): dependency}))
    await queue.put(_QueuedRun(next_run_id, "DUT-2", next_plan, {}))
    while station._find_active_run_by_id(active_runs, waiting_run_id) is None:
        await asyncio.sleep(0)

    station._find_active_run_by_id(active_runs, waiting_run_id).cancel()  # type: ignore[union-attr]
    await asyncio.wait_for(queue.join(), timeout=1)

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    results = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert [(result["runId"], result["outcome"]) for result in results] == [
        (waiting_run_id, "aborted"),
        (next_run_id, "passed"),
    ]


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


async def test_unexpected_stage_failure_publishes_terminal_error_and_resolves_stage_future() -> None:
    station, publisher = make_station(), Publisher()
    station._stage_factories["error"] = ErrorStage
    stage = StageSettings(name="Flash", kind="error")
    future = asyncio.get_running_loop().create_future()

    result = await station._run_test(
        publisher,
        "DUT",
        str(uuid4()),
        stages=(stage,),
        lane="left",
        batch_results={("left", "Flash"): future},
    )

    assert result.outcome == "error"
    assert future.result() == "error"
    terminal = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert terminal[-1]["outcome"] == "error"
