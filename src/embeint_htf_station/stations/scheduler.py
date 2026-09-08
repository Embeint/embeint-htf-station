from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

import structlog

from embeint_htf_station.config import StageSettings

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunPlan:
    lane: str
    stages: tuple[StageSettings, ...]


@dataclass(frozen=True)
class QueuedRun:
    run_id: str | None
    dut_id: str
    plan: RunPlan
    batch_results: Mapping[tuple[str, str], asyncio.Future[str]]
    command_id: UUID | None = None


@dataclass
class LaneActivity:
    lane: str
    run_id: str | None
    dut_id: str
    status: str = "queued"
    current_stage: str | None = None
    waiting_reason: str | None = None


@dataclass(frozen=True)
class ActiveRun:
    run_id: str | None
    task: asyncio.Task[object]


RunTest = Callable[..., Awaitable[object]]
PublishAborted = Callable[[object, str | None, str, str], Awaitable[None]]
RunCompleted = Callable[[QueuedRun], None]


class LaneScheduler:
    def __init__(
        self,
        client: object,
        plans: Mapping[str, RunPlan],
        run_test: RunTest,
        publish_aborted: PublishAborted,
        run_completed: RunCompleted | None = None,
    ) -> None:
        self.queues: dict[str, asyncio.Queue[QueuedRun]] = {
            lane: asyncio.Queue() for lane in plans
        }
        self.active_runs: dict[str, ActiveRun] = {}
        self.active_lanes: dict[str, LaneActivity] = {}
        self._client = client
        self._run_test = run_test
        self._publish_aborted = publish_aborted
        self._run_completed = run_completed
        self._workers: dict[str, asyncio.Task[None]] = {}

    def start(self) -> None:
        self._workers = {
            lane: asyncio.create_task(self._worker(lane, queue))
            for lane, queue in self.queues.items()
        }

    async def stop(self) -> None:
        for worker in self._workers.values():
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)

    async def enqueue(self, queued: QueuedRun) -> None:
        await self.queues[queued.plan.lane].put(queued)

    def find_active(self, run_id: object) -> asyncio.Task[object] | None:
        if not isinstance(run_id, str):
            return None
        for active in self.active_runs.values():
            if not active.task.done() and active.run_id == run_id:
                return active.task
        return None

    def state(self) -> tuple[str, str | None, tuple[LaneActivity, ...]]:
        for active in self.active_runs.values():
            if not active.task.done():
                return "running", active.run_id, tuple(self.active_lanes.values())
        return "idle", None, ()

    async def abort(self, run_id: object) -> bool:
        active = self.find_active(run_id)
        if active is not None:
            active.cancel()
            return True
        return await abort_queued_run(
            self._client,
            self.queues,
            run_id,
            self._publish_aborted,
            self._run_completed,
        )

    async def _worker(self, lane: str, queue: asyncio.Queue[QueuedRun]) -> None:
        await run_lane_worker(
            self._client,
            lane,
            queue,
            self.active_runs,
            self.active_lanes,
            self._run_test,
            self._publish_aborted,
            self._run_completed,
        )


async def run_lane_worker(
    client: object,
    lane: str,
    queue: asyncio.Queue[QueuedRun],
    active_runs: dict[str, ActiveRun],
    active_lanes: dict[str, LaneActivity],
    run_test: RunTest,
    publish_aborted: PublishAborted,
    run_completed: RunCompleted | None = None,
) -> None:
    while True:
        queued = await queue.get()
        activity = LaneActivity(lane=lane, run_id=queued.run_id, dut_id=queued.dut_id, status="running")
        active_lanes[lane] = activity
        task = asyncio.create_task(run_test(
            client,
            queued.dut_id,
            queued.run_id,
            stages=queued.plan.stages,
            lane=lane,
            publish_idle_on_finish=False,
            batch_results=queued.batch_results,
            activity=activity,
        ))
        active = ActiveRun(queued.run_id, task)
        active_runs[lane] = active
        try:
            result = await task
            outcome = getattr(result, "outcome", "error")
            complete_run_future(queued, outcome)
            if run_completed is not None:
                run_completed(queued)
        except asyncio.CancelledError:
            complete_run_futures(queued, "aborted")
            if task.cancelled():
                await publish_aborted(client, queued.run_id, queued.dut_id, lane)
                if run_completed is not None:
                    run_completed(queued)
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise
        except Exception:
            complete_run_future(queued, "error")
            log.exception("lane_scheduler.run_task_failed")
        finally:
            if active_runs.get(lane) is active:
                active_runs.pop(lane, None)
            if active_lanes.get(lane) is activity:
                active_lanes.pop(lane, None)
            queue.task_done()
        if asyncio.current_task() is not None and asyncio.current_task().cancelling():
            return


async def abort_queued_run(
    client: object,
    queues: Mapping[str, asyncio.Queue[QueuedRun]],
    run_id: object,
    publish_aborted: PublishAborted,
    run_completed: RunCompleted | None = None,
) -> bool:
    if not isinstance(run_id, str):
        return False
    for queue in queues.values():
        retained: list[QueuedRun] = []
        aborted: QueuedRun | None = None
        while not queue.empty():
            item = queue.get_nowait()
            queue.task_done()
            if aborted is None and item.run_id == run_id:
                aborted = item
            else:
                retained.append(item)
        for item in retained:
            await queue.put(item)
        if aborted is not None:
            await publish_aborted(client, aborted.run_id, aborted.dut_id, aborted.plan.lane)
            complete_run_futures(aborted, "aborted")
            if run_completed is not None:
                run_completed(aborted)
            return True
    return False


def complete_run_future(queued: QueuedRun, outcome: str) -> None:
    future = queued.batch_results.get((queued.plan.lane, "__run__"))
    if future is not None and not future.done():
        future.set_result(outcome)


def complete_run_futures(queued: QueuedRun, outcome: str) -> None:
    for key, future in queued.batch_results.items():
        if key[0] == queued.plan.lane and not future.done():
            future.set_result(outcome)


def active_run_state(
    active_runs: Mapping[str, ActiveRun],
    active_lanes: Mapping[str, LaneActivity],
) -> tuple[str, str | None, tuple[LaneActivity, ...]]:
    for active in active_runs.values():
        if not active.task.done():
            return "running", active.run_id, tuple(active_lanes.values())
    return "idle", None, ()


def find_active_run_by_id(
    active_runs: Mapping[str, ActiveRun],
    run_id: object,
) -> asyncio.Task[object] | None:
    if not isinstance(run_id, str):
        return None
    for active in active_runs.values():
        if not active.task.done() and active.run_id == run_id:
            return active.task
    return None
