from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog

from embeint_htf_station.config import Settings, StageSettings
from embeint_htf_station.contracts.mqtt import Stage, TestResult as MqttTestResult, TestResultStagesItem
from embeint_htf_station.messaging.batch_logger import BatchLogger
from embeint_htf_station.stages import StageContext, StageFactory, StageResult, create_stage
from embeint_htf_station.stations.scheduler import LaneActivity

log = structlog.get_logger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class Publisher(Protocol):
    async def publish(self, topic: str, payload: str, qos: int = 0) -> None: ...


@dataclass(frozen=True)
class TestResult:
    run_id: str | None
    dut_id: str
    outcome: str
    config_revision: int | None
    started_at: datetime
    finished_at: datetime
    stages: tuple[StageResult, ...]


class StageScopedLogger:
    def __init__(
        self,
        logger: BatchLogger,
        stage_name: str,
        *,
        dut_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self._logger = logger
        self._stage_name = stage_name
        self.dut_id = dut_id
        self.run_id = run_id

    async def start(self) -> None:
        formatted = f"======={self._stage_name}======="
        print(formatted)
        await self._logger.log("info", formatted)

    async def log(self, level: str, msg: str) -> None:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        msg = sanitize_log_text(msg)
        formatted = f"[{timestamp}][{self._stage_name}] - {msg}"
        print(formatted)
        await self._logger.log(level, formatted)


def sanitize_log_text(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value).replace("\x00", "")


HeartbeatPublisher = Callable[
    [Publisher, str, str | None, Sequence[LaneActivity] | None],
    Awaitable[None],
]


class StageRunner:
    def __init__(
        self,
        settings: Settings,
        stage_factories: Mapping[str, StageFactory],
        stage_locks: dict[str, asyncio.Lock],
        get_config_revision: Callable[[], int | None],
        publish_heartbeat: HeartbeatPublisher,
    ) -> None:
        self._settings = settings
        self._stage_factories = stage_factories
        self._stage_locks = stage_locks
        self._get_config_revision = get_config_revision
        self._publish_heartbeat = publish_heartbeat

    async def run(
        self,
        client: Publisher,
        dut_id: str,
        run_id: str | None,
        *,
        stages: Sequence[StageSettings],
        lane: str = "default",
        publish_idle_on_finish: bool = True,
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None = None,
        activity: LaneActivity | None = None,
    ) -> TestResult:
        started_at = datetime.now(UTC)
        logger = BatchLogger(client, f"{self._settings.topic_prefix}/log", run_id=run_id, lane=lane)
        run_stages = tuple(stages)
        completed_stages: list[StageResult] = []
        current_index = 0
        await logger.start()
        try:
            await self._publish_heartbeat(client, "running", run_id, [activity] if activity else None)
            await logger.log("info", f"starting basic test for DUT {dut_id} on lane {lane}")
            for index, stage in enumerate(run_stages):
                await self.publish_stage_update(client, lane, run_id, index, stage.name, "pending")

            run_context = StageContext(dut_id=dut_id, run_id=run_id)
            for index, stage_settings in enumerate(run_stages):
                current_index = index
                dependency_failure = await self._wait_for_dependencies(
                    client,
                    logger,
                    lane,
                    run_id,
                    index,
                    stage_settings,
                    run_stages,
                    batch_results,
                    activity,
                    completed_stages,
                    started_at,
                    dut_id,
                )
                if dependency_failure is not None:
                    return dependency_failure

                if activity is not None:
                    activity.status = "running"
                    activity.current_stage = stage_settings.name
                    activity.waiting_reason = None
                await self.publish_stage_update(client, lane, run_id, index, stage_settings.name, "running")
                stage_result = await self._execute_stage(
                    client,
                    logger,
                    run_context,
                    lane,
                    run_id,
                    index,
                    stage_settings,
                    activity,
                )
                completed_stages.append(stage_result)
                run_context.prerequisites_passed &= stage_result.outcome == "passed"
                complete_stage_future(batch_results, lane, stage_result.name, stage_result.outcome)
                await self.publish_stage_update(client, lane, run_id, index, stage_result.name, stage_result.outcome)
                if stage_result.outcome != "passed" and any(s.kind == "commit_variables" for s in run_stages):
                    await self._abort_remaining(client, lane, run_id, run_stages, index + 1, batch_results)
                    result = self._result(run_id, dut_id, "failed", started_at, completed_stages)
                    await self.publish_result(client, result, lane)
                    return result

            outcome = "passed" if all(stage.outcome == "passed" for stage in completed_stages) else "failed"
            result = self._result(run_id, dut_id, outcome, started_at, completed_stages)
            await logger.log("info", f"basic test {outcome} for DUT {dut_id}")
            await self.publish_result(client, result, lane)
            log.info("stage_runner.finished", dut_id=dut_id, outcome=outcome)
            return result
        except asyncio.CancelledError:
            return await self._finish_interrupted(
                client,
                logger,
                run_id,
                dut_id,
                lane,
                run_stages,
                current_index,
                completed_stages,
                batch_results,
                started_at,
                "aborted",
                "basic test aborted",
            )
        except Exception as exception:
            log.exception("stage_runner.failed", run_id=run_id, dut_id=dut_id)
            return await self._finish_interrupted(
                client,
                logger,
                run_id,
                dut_id,
                lane,
                run_stages,
                current_index,
                completed_stages,
                batch_results,
                started_at,
                "error",
                f"basic test failed: {exception}",
            )
        finally:
            await logger.stop()
            if publish_idle_on_finish:
                await self._publish_heartbeat(client, "idle", None, None)

    async def _wait_for_dependencies(
        self,
        client: Publisher,
        logger: BatchLogger,
        lane: str,
        run_id: str | None,
        index: int,
        stage_settings: StageSettings,
        run_stages: tuple[StageSettings, ...],
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None,
        activity: LaneActivity | None,
        completed_stages: list[StageResult],
        started_at: datetime,
        dut_id: str,
    ) -> TestResult | None:
        dependencies = stage_settings.after if batch_results is not None or stage_settings.kind == "commit_variables" else ()
        for dependency in dependencies:
            if activity is not None:
                activity.status = "blocked"
                activity.current_stage = stage_settings.name
                activity.waiting_reason = f"Waiting for {dependency.lane}.{dependency.stage}"
            await self.publish_stage_update(client, lane, run_id, index, stage_settings.name, "blocked")
            if dependency.lane == lane:
                # This lane runs sequentially, including outside a batch. Only an
                # already completed stage can satisfy one of its prerequisites.
                prerequisite_outcome = next(
                    (stage.outcome for stage in completed_stages if stage.name == dependency.stage), None,
                )
            else:
                prerequisite = batch_results.get((dependency.lane, dependency.stage)) if batch_results is not None else None
                prerequisite_outcome = await asyncio.shield(prerequisite) if prerequisite is not None else None
            if activity is not None:
                activity.status = "running"
                activity.waiting_reason = None
            if prerequisite_outcome == dependency.outcome and (stage_settings.kind != "commit_variables" or prerequisite_outcome == "passed"):
                continue

            now = datetime.now(UTC)
            failed = StageResult(stage_settings.name, "failed", now, now)
            completed_stages.append(failed)
            complete_stage_future(batch_results, lane, failed.name, "failed")
            await self.publish_stage_update(client, lane, run_id, index, failed.name, "failed")
            await self._abort_remaining(client, lane, run_id, run_stages, index + 1, batch_results)
            result = self._result(run_id, dut_id, "failed", started_at, completed_stages, now)
            await logger.log("error", f"dependency {dependency.lane}.{dependency.stage} did not reach {dependency.outcome}")
            await self.publish_result(client, result, lane)
            return result
        return None

    async def _execute_stage(
        self,
        client: Publisher,
        logger: BatchLogger,
        run_context: StageContext,
        lane: str,
        run_id: str | None,
        index: int,
        stage_settings: StageSettings,
        activity: LaneActivity | None,
    ) -> StageResult:
        stage_logger = StageScopedLogger(logger, stage_settings.name, dut_id=run_context.dut_id, run_id=run_id)
        await stage_logger.start()
        lock_names = sorted(stage_settings.locks)
        locks = [self._stage_locks.setdefault(name, asyncio.Lock()) for name in lock_names]
        acquired_locks: list[asyncio.Lock] = []
        try:
            for lock_name, lock in zip(lock_names, locks, strict=True):
                if lock.locked():
                    if activity is not None:
                        activity.status = "blocked"
                        activity.waiting_reason = f"Waiting for resource lock {lock_name}"
                    await self.publish_stage_update(client, lane, run_id, index, stage_settings.name, "blocked")
                await lock.acquire()
                acquired_locks.append(lock)
            if activity is not None:
                activity.status = "running"
                activity.waiting_reason = None
            return await create_stage(stage_settings, self._stage_factories).run(stage_logger, run_context)
        finally:
            for lock in reversed(acquired_locks):
                lock.release()

    async def _finish_interrupted(
        self,
        client: Publisher,
        logger: BatchLogger,
        run_id: str | None,
        dut_id: str,
        lane: str,
        run_stages: tuple[StageSettings, ...],
        current_index: int,
        completed_stages: list[StageResult],
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None,
        started_at: datetime,
        outcome: str,
        message: str,
    ) -> TestResult:
        status = "aborted" if outcome == "aborted" else "error"
        await self._mark_remaining(client, lane, run_id, run_stages, current_index, batch_results, status)
        result = self._result(run_id, dut_id, outcome, started_at, completed_stages)
        await logger.log("warning" if outcome == "aborted" else "error", f"{message} for DUT {dut_id}")
        await self.publish_result(client, result, lane)
        log.info("stage_runner.finished", dut_id=dut_id, outcome=outcome)
        return result

    async def _mark_remaining(
        self,
        client: Publisher,
        lane: str,
        run_id: str | None,
        run_stages: tuple[StageSettings, ...],
        start_index: int,
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None,
        first_status: str,
    ) -> None:
        for index, stage in enumerate(run_stages[start_index:], start_index):
            status = first_status if index == start_index else "aborted"
            complete_stage_future(batch_results, lane, stage.name, status)
            await self.publish_stage_update(client, lane, run_id, index, stage.name, status)

    async def _abort_remaining(
        self,
        client: Publisher,
        lane: str,
        run_id: str | None,
        run_stages: tuple[StageSettings, ...],
        start_index: int,
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None,
    ) -> None:
        await self._mark_remaining(client, lane, run_id, run_stages, start_index, batch_results, "aborted")

    def _result(
        self,
        run_id: str | None,
        dut_id: str,
        outcome: str,
        started_at: datetime,
        stages: Sequence[StageResult],
        finished_at: datetime | None = None,
    ) -> TestResult:
        return TestResult(
            run_id,
            dut_id,
            outcome,
            self._get_config_revision(),
            started_at,
            finished_at or datetime.now(UTC),
            tuple(stages),
        )

    async def publish_aborted(self, client: Publisher, run_id: str | None, dut_id: str, lane: str) -> None:
        now = datetime.now(UTC)
        await self.publish_result(
            client,
            TestResult(run_id, dut_id, "aborted", self._get_config_revision(), now, now, ()),
            lane,
        )

    async def publish_terminal_error(
        self,
        client: Publisher,
        run_id: str,
        dut_id: str,
        reason: str,
        lane: str = "default",
    ) -> None:
        now = datetime.now(UTC)
        logger = BatchLogger(client, f"{self._settings.topic_prefix}/log", run_id=run_id, lane=lane)
        await logger.start()
        try:
            await logger.log("error", reason)
        finally:
            await logger.stop()
        await self.publish_result(
            client,
            TestResult(run_id, dut_id, "error", self._get_config_revision(), now, now, ()),
            lane,
        )

    async def publish_stage_update(
        self,
        client: Publisher,
        lane: str,
        run_id: str | None,
        index: int,
        name: str,
        status: str,
    ) -> None:
        payload = Stage(
            ts=datetime.now(UTC),
            runId=run_id,
            lane=lane,
            index=index,
            name=name,
            status=status,
        ).model_dump_json(by_alias=True)
        await client.publish(f"{self._settings.topic_prefix}/stage", payload=payload, qos=1)

    async def publish_result(self, client: Publisher, result: TestResult, lane: str) -> None:
        payload = MqttTestResult(
            ts=result.finished_at,
            runId=result.run_id,
            lane=lane,
            dutId=result.dut_id,
            outcome=result.outcome,
            configRevision=result.config_revision,
            startedAt=result.started_at,
            finishedAt=result.finished_at,
            stages=[
                TestResultStagesItem(
                    name=stage.name,
                    outcome=stage.outcome,
                    startedAt=stage.started_at,
                    finishedAt=stage.finished_at,
                )
                for stage in result.stages
            ],
        ).model_dump_json(by_alias=True)
        await client.publish(f"{self._settings.topic_prefix}/result", payload=payload, qos=1)


def complete_stage_future(
    results: Mapping[tuple[str, str], asyncio.Future[str]] | None,
    lane: str,
    stage: str,
    outcome: str,
) -> None:
    if results is None:
        return
    future = results.get((lane, stage))
    if future is not None and not future.done():
        future.set_result(outcome)
