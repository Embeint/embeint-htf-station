from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

from embeint_htf_station.config import (
    ConfigError, Settings, StageSettings,
    parse_runtime_plans_from_yaml_text,
)
from embeint_htf_station.contracts.mqtt import (
    Command,
    Heartbeat,
    HeartbeatActiveLanesItem,
    Stage,
    TestResult as MqttTestResult,
    TestResultStagesItem,
)
from embeint_htf_station.firmware import FirmwareCache
from embeint_htf_station.messaging.batch_logger import BatchLogger
from embeint_htf_station.messaging.client import connect
from embeint_htf_station.stages import StageContext, StageFactory, StageResult, create_stage, default_stage_factories
from embeint_htf_station.stages.hardware_id import HardwareIdStage
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage
from embeint_htf_station.stages.infuse_validation import InfuseValidationHook, InfuseValidationStage
from embeint_htf_station.stages.nrfutil import FirmwareFlashStage, NrfutilDeviceRecoverStage, NrfutilDeviceResetStage

log = structlog.get_logger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _Publisher(Protocol):
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


@dataclass(frozen=True)
class RuntimeConfiguration:
    revision: int
    yaml: str


@dataclass(frozen=True)
class _RunPlan:
    lane: str
    stages: tuple[StageSettings, ...]


@dataclass(frozen=True)
class _QueuedRun:
    run_id: str | None
    dut_id: str
    plan: _RunPlan
    batch_results: Mapping[tuple[str, str], asyncio.Future[str]]


@dataclass
class _LaneActivity:
    lane: str
    run_id: str | None
    dut_id: str
    status: str = "queued"
    current_stage: str | None = None
    waiting_reason: str | None = None


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
        msg = _sanitize_log_text(msg)
        formatted = f"[{timestamp}][{self._stage_name}] - {msg}"
        print(formatted)
        await self._logger.log(level, formatted)


def _sanitize_log_text(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value).replace("\x00", "")


def _task_run_id(task: asyncio.Task[TestResult]) -> str | None:
    value = getattr(task, "run_id", None)
    return value if isinstance(value, str) else None


class BasicStation:
    """Minimal station used to validate the server-to-station pipeline."""

    def __init__(
        self,
        settings: Settings,
        stage_factories: Mapping[str, StageFactory] | None = None,
        infuse_validation_hooks: Sequence[InfuseValidationHook] = (),
    ) -> None:
        self._settings = settings
        self._stages = list(settings.stages)
        self._plans = {
            plan.lane: _RunPlan(lane=plan.lane, stages=plan.stages)
            for plan in settings.plans
        } or {
            "default": _RunPlan(lane="default", stages=tuple(settings.stages)),
        }
        self._runtime_config_revision: int | None = None
        self._stage_locks: dict[str, asyncio.Lock] = {}
        self._programmers = {programmer.name: programmer for programmer in settings.programmers}
        self._firmware_cache = FirmwareCache(settings)
        self._stage_factories = default_stage_factories()
        self._stage_factories.update({
            "nrfutil_device_recover": lambda stage: NrfutilDeviceRecoverStage(stage, self._programmers),
            "nrfutil_reset": lambda stage: NrfutilDeviceResetStage(stage, self._programmers),
            "firmware_flash": lambda stage: FirmwareFlashStage(stage, self._programmers, self._firmware_cache),
            "hardware_id": lambda stage: HardwareIdStage(stage, self._programmers),
            "infuse_validation": lambda stage: InfuseValidationStage(stage, self._programmers, infuse_validation_hooks),
            "infuse_validation_rtt": lambda stage: InfuseValidationStage(stage, self._programmers, infuse_validation_hooks),
            "infuse_provisioning": lambda stage: InfuseProvisioningStage(stage, self._programmers, self._settings),
        })
        if stage_factories:
            self._stage_factories.update(stage_factories)

    async def run_once(self, dut_id: str) -> TestResult:
        await self._load_runtime_configuration()
        plan = self._default_run_plan()
        async with connect(self._settings) as client:
            return await self._run_test(client, dut_id, run_id=None, stages=plan.stages, lane=plan.lane)

    async def serve_forever(self) -> None:
        await self._load_runtime_configuration()
        async with connect(self._settings) as client:
            await client.subscribe(f"{self._settings.topic_prefix}/cmd", qos=1)
            log.info("basic_station.subscribed", topic=f"{self._settings.topic_prefix}/cmd")

            active_runs: dict[str, asyncio.Task[TestResult]] = {}
            active_lanes: dict[str, _LaneActivity] = {}
            lane_queues: dict[str, asyncio.Queue[_QueuedRun]] = {lane: asyncio.Queue() for lane in self._plans}
            lane_workers: dict[str, asyncio.Task[None]] = {
                lane: asyncio.create_task(self._lane_worker(client, lane, queue, active_runs, active_lanes))
                for lane, queue in lane_queues.items()
            }
            heartbeat_task = asyncio.create_task(
                self._serve_heartbeat_loop(
                    client,
                    lambda: self._active_run_state(active_runs, active_lanes),
                ),
            )

            try:
                async for message in client.messages:
                    command = self._parse_command(message.payload)
                    if command is None:
                        continue

                    payload = command.payload
                    if not isinstance(payload, dict):
                        log.warning("basic_station.command_missing_payload")
                        continue

                    if command.kind == "abort-run":
                        run_id = payload.get("runId")
                        task = self._find_active_run_by_id(active_runs, run_id)
                        if task is not None:
                            log.info("basic_station.abort_requested", run_id=run_id)
                            task.cancel()
                        else:
                            await self._abort_queued_run(client, lane_queues, run_id)
                        continue

                    if command.kind == "run-batch":
                        await self._enqueue_batch(client, payload, lane_queues)
                        continue

                    if command.kind != "run-plan":
                        log.info("basic_station.command_ignored", kind=command.kind)
                        continue

                    dut_id = payload.get("dutId")
                    if not isinstance(dut_id, str) or not dut_id.strip():
                        log.warning("basic_station.command_missing_dut_id")
                        if isinstance(payload.get("runId"), str):
                            await self._publish_terminal_error(client, payload["runId"], "unknown", "command missing DUT ID")
                        continue

                    plan = self._run_plan_from_payload(payload)
                    if plan is None:
                        if isinstance(payload.get("runId"), str):
                            await self._publish_terminal_error(client, payload["runId"], dut_id.strip(), "invalid or stale lane command")
                        continue
                    run_id = payload.get("runId")
                    current_run_id = run_id if isinstance(run_id, str) else None
                    await lane_queues[plan.lane].put(_QueuedRun(current_run_id, dut_id.strip(), plan, {}))
            finally:
                heartbeat_task.cancel()
                for task in active_runs.values():
                    task.cancel()
                for worker in lane_workers.values():
                    worker.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                if active_runs:
                    await asyncio.gather(*active_runs.values(), return_exceptions=True)
                await asyncio.gather(*lane_workers.values(), return_exceptions=True)

    async def _enqueue_batch(
        self, client: _Publisher, payload: dict[str, object], lane_queues: Mapping[str, asyncio.Queue[_QueuedRun]],
    ) -> None:
        entries = payload.get("runs")
        if not isinstance(entries, list):
            log.warning("basic_station.batch_invalid", error="runs is required")
            return
        futures: dict[tuple[str, str], asyncio.Future[str]] = {}
        parsed: list[tuple[str, str, str]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            run_id, lane, dut_id = item.get("runId"), item.get("lane"), item.get("dutId")
            if not all(isinstance(value, str) and value.strip() for value in (run_id, lane, dut_id)) or lane not in self._plans:
                if isinstance(run_id, str) and isinstance(dut_id, str):
                    await self._publish_terminal_error(client, run_id, dut_id, "invalid batch lane or DUT")
                continue
            if (lane, "__run__") in futures:
                await self._publish_terminal_error(client, run_id, dut_id, "duplicate lane in batch")
                continue
            futures[(lane, "__run__")] = asyncio.get_running_loop().create_future()
            for stage in self._plans[lane].stages:
                futures[(lane, stage.name)] = asyncio.get_running_loop().create_future()
            parsed.append((run_id, lane, dut_id))
        for run_id, lane, dut_id in parsed:
            await lane_queues[lane].put(_QueuedRun(run_id, dut_id, self._plans[lane], futures))

    async def _lane_worker(
        self,
        client: _Publisher,
        lane: str,
        queue: asyncio.Queue[_QueuedRun],
        active_runs: dict[str, asyncio.Task[TestResult]],
        active_lanes: dict[str, _LaneActivity],
    ) -> None:
        while True:
            queued = await queue.get()
            activity = _LaneActivity(lane=lane, run_id=queued.run_id, dut_id=queued.dut_id, status="running")
            active_lanes[lane] = activity
            task = asyncio.create_task(self._run_test(
                client, queued.dut_id, queued.run_id, stages=queued.plan.stages, lane=lane,
                publish_idle_on_finish=False, batch_results=queued.batch_results, activity=activity,
            ))
            setattr(task, "run_id", queued.run_id)
            active_runs[lane] = task
            try:
                result = await task
                future = queued.batch_results.get((lane, "__run__"))
                if future is not None and not future.done():
                    future.set_result(result.outcome)
            except asyncio.CancelledError:
                future = queued.batch_results.get((lane, "__run__"))
                if future is not None and not future.done():
                    future.set_result("aborted")
                raise
            except Exception:
                future = queued.batch_results.get((lane, "__run__"))
                if future is not None and not future.done():
                    future.set_result("error")
                log.exception("basic_station.run_task_failed")
            finally:
                if active_runs.get(lane) is task:
                    active_runs.pop(lane, None)
                if active_lanes.get(lane) is activity:
                    active_lanes.pop(lane, None)
                queue.task_done()

    async def _abort_queued_run(self, client: _Publisher, queues: Mapping[str, asyncio.Queue[_QueuedRun]], run_id: object) -> None:
        if not isinstance(run_id, str):
            return
        for queue in queues.values():
            retained: list[_QueuedRun] = []
            while not queue.empty():
                item = queue.get_nowait()
                queue.task_done()
                if item.run_id == run_id:
                    await self._publish_aborted_without_stages(client, item.run_id, item.dut_id, item.plan.lane)
                    future = item.batch_results.get((item.plan.lane, "__run__"))
                    if future is not None and not future.done():
                        future.set_result("aborted")
                    return
                retained.append(item)
            for item in retained:
                await queue.put(item)

    async def _publish_aborted_without_stages(
        self, client: _Publisher, run_id: str | None, dut_id: str, lane: str,
    ) -> None:
        now = datetime.now(UTC)
        await self._publish_result(
            client,
            TestResult(run_id, dut_id, "aborted", self._runtime_config_revision, now, now, ()),
            lane,
        )

    async def _publish_terminal_error(
        self, client: _Publisher, run_id: str, dut_id: str, reason: str, lane: str = "default",
    ) -> None:
        now = datetime.now(UTC)
        logger = BatchLogger(client, f"{self._settings.topic_prefix}/log", run_id=run_id, lane=lane)
        await logger.start()
        await logger.log("error", reason)
        await logger.stop()
        await self._publish_result(
            client,
            TestResult(run_id, dut_id, "error", self._runtime_config_revision, now, now, ()),
            lane,
        )

    async def _run_test(
        self,
        client: _Publisher,
        dut_id: str,
        run_id: str | None,
        *,
        stages: Sequence[StageSettings] | None = None,
        lane: str = "default",
        publish_idle_on_finish: bool = True,
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] = {},
        activity: _LaneActivity | None = None,
    ) -> TestResult:
        started_at = datetime.now(UTC)
        logger = BatchLogger(client, f"{self._settings.topic_prefix}/log", run_id=run_id, lane=lane)
        run_stages = tuple(stages) if stages is not None else tuple(self._stages)
        await logger.start()
        try:
            await self._publish_heartbeat(client, "running", run_id=run_id, active_lanes=[activity] if activity else None)
            await logger.log("info", f"starting basic test for DUT {dut_id} on lane {lane}")
            for index, stage in enumerate(run_stages):
                await self._publish_stage_update(client, lane, run_id, index, stage.name, "pending")

            run_context = StageContext(dut_id=dut_id, run_id=run_id)
            stages: list[StageResult] = []
            for index, stage_settings in enumerate(run_stages):
                for dependency in stage_settings.after:
                    prerequisite = batch_results.get((dependency.lane, dependency.stage))
                    if activity is not None:
                        activity.status = "blocked"
                        activity.current_stage = stage_settings.name
                        activity.waiting_reason = f"Waiting for {dependency.lane}.{dependency.stage}"
                    await self._publish_stage_update(client, lane, run_id, index, stage_settings.name, "blocked")
                    prerequisite_outcome = await prerequisite if prerequisite is not None else None
                    if activity is not None:
                        activity.status = "running"
                        activity.waiting_reason = None
                    if prerequisite_outcome != dependency.outcome:
                        now = datetime.now(UTC)
                        failed = StageResult(stage_settings.name, "failed", now, now)
                        stages.append(failed)
                        self._complete_stage_future(batch_results, lane, failed.name, "failed")
                        await self._publish_stage_update(client, lane, run_id, index, failed.name, "failed")
                        for remaining_index, remaining in enumerate(run_stages[index + 1:], index + 1):
                            self._complete_stage_future(batch_results, lane, remaining.name, "aborted")
                            await self._publish_stage_update(client, lane, run_id, remaining_index, remaining.name, "aborted")
                        result = TestResult(run_id, dut_id, "failed", self._runtime_config_revision, started_at, now, tuple(stages))
                        await logger.log("error", f"dependency {dependency.lane}.{dependency.stage} did not reach {dependency.outcome}")
                        await self._publish_result(client, result, lane)
                        return result
                if activity is not None:
                    activity.status = "running"
                    activity.current_stage = stage_settings.name
                    activity.waiting_reason = None
                await self._publish_stage_update(client, lane, run_id, index, stage_settings.name, "running")
                stage_logger = StageScopedLogger(
                    logger,
                    stage_settings.name,
                    dut_id=dut_id,
                    run_id=run_id,
                )
                await stage_logger.start()
                stage_started_at = datetime.now(UTC)
                try:
                    lock_names = sorted(stage_settings.locks)
                    locks = [self._stage_locks.setdefault(name, asyncio.Lock()) for name in lock_names]
                    for lock_name, lock in zip(lock_names, locks, strict=True):
                        if lock.locked():
                            if activity is not None:
                                activity.status = "blocked"
                                activity.waiting_reason = f"Waiting for resource lock {lock_name}"
                            await self._publish_stage_update(client, lane, run_id, index, stage_settings.name, "blocked")
                        await lock.acquire()
                    if activity is not None:
                        activity.status = "running"
                        activity.waiting_reason = None
                    try:
                        stage_result = await create_stage(stage_settings, self._stage_factories).run(stage_logger, run_context)
                    finally:
                        for lock in reversed(locks):
                            lock.release()
                except asyncio.CancelledError:
                    finished_at = datetime.now(UTC)
                    await stage_logger.log("warning", "stage aborted")
                    stage_result = StageResult(
                        name=stage_settings.name,
                        outcome="aborted",
                        started_at=stage_started_at,
                        finished_at=finished_at,
                    )
                    stages.append(stage_result)
                    self._complete_stage_future(batch_results, lane, stage_result.name, stage_result.outcome)
                    await self._publish_stage_update(client, lane, run_id, index, stage_result.name, stage_result.outcome)
                    for remaining_index, remaining in enumerate(run_stages[index + 1:], index + 1):
                        self._complete_stage_future(batch_results, lane, remaining.name, "aborted")
                        await self._publish_stage_update(client, lane, run_id, remaining_index, remaining.name, "aborted")
                    result = TestResult(
                        run_id=run_id,
                        dut_id=dut_id,
                        outcome="aborted",
                        config_revision=self._runtime_config_revision,
                        started_at=started_at,
                        finished_at=finished_at,
                        stages=tuple(stages),
                    )
                    await logger.log("warning", f"basic test aborted for DUT {dut_id}")
                    await self._publish_result(client, result, lane)
                    log.info("basic_test.finished", dut_id=dut_id, outcome="aborted")
                    return result
                stages.append(stage_result)
                self._complete_stage_future(batch_results, lane, stage_result.name, stage_result.outcome)
                await self._publish_stage_update(client, lane, run_id, index, stage_result.name, stage_result.outcome)

            outcome = "passed" if all(stage.outcome == "passed" for stage in stages) else "failed"

            finished_at = datetime.now(UTC)
            result = TestResult(
                run_id=run_id,
                dut_id=dut_id,
                outcome=outcome,
                config_revision=self._runtime_config_revision,
                started_at=started_at,
                finished_at=finished_at,
                stages=tuple(stages),
            )
            await logger.log("info", f"basic test {outcome} for DUT {dut_id}")
            await self._publish_result(client, result, lane)
            log.info("basic_test.finished", dut_id=dut_id, outcome=outcome)
            return result
        finally:
            await logger.stop()
            if publish_idle_on_finish:
                await self._publish_heartbeat(client, "idle")

    def _default_run_plan(self) -> _RunPlan:
        if len(self._plans) == 1:
            return next(iter(self._plans.values()))
        if "default" in self._plans:
            return self._plans["default"]
        raise ConfigError("multiple station lanes are configured; run command requires a lane")

    @staticmethod
    def _complete_stage_future(
        results: Mapping[tuple[str, str], asyncio.Future[str]], lane: str, stage: str, outcome: str,
    ) -> None:
        future = results.get((lane, stage))
        if future is not None and not future.done():
            future.set_result(outcome)

    def _run_plan_from_payload(self, payload: dict[str, object]) -> _RunPlan | None:
        requested_lane = payload.get("lane")
        if requested_lane is None or requested_lane == "":
            try:
                return self._default_run_plan()
            except ConfigError as exc:
                log.warning("basic_station.command_missing_lane", error=str(exc))
                return None
        if not isinstance(requested_lane, str):
            log.warning("basic_station.command_invalid_lane", lane=requested_lane)
            return None

        plan = self._plans.get(requested_lane.strip())
        if plan is None:
            log.warning("basic_station.command_unknown_lane", lane=requested_lane)
            return None
        return plan

    @staticmethod
    def _active_run_state(
        active_runs: Mapping[str, asyncio.Task[TestResult]],
        active_lanes: Mapping[str, _LaneActivity],
    ) -> tuple[str, str | None, tuple[_LaneActivity, ...]]:
        for task in active_runs.values():
            if not task.done():
                return "running", _task_run_id(task), tuple(active_lanes.values())
        return "idle", None, ()

    @staticmethod
    def _find_active_run_by_id(
        active_runs: Mapping[str, asyncio.Task[TestResult]],
        run_id: object,
    ) -> asyncio.Task[TestResult] | None:
        if not isinstance(run_id, str):
            return None
        for task in active_runs.values():
            if not task.done() and _task_run_id(task) == run_id:
                return task
        return None

    def _log_and_forget_lane_task(
        self,
        active_runs: dict[str, asyncio.Task[TestResult]],
        lane: str,
        task: asyncio.Task[TestResult],
    ) -> None:
        self._log_run_task_result(task)
        if active_runs.get(lane) is task:
            active_runs.pop(lane, None)

    @staticmethod
    def _log_run_task_result(task: asyncio.Task[TestResult]) -> None:
        if task.cancelled():
            log.warning("basic_station.run_task_cancelled")
            return
        exc = task.exception()
        if exc is not None:
            log.error("basic_station.run_task_failed", error=str(exc))

    @staticmethod
    def _parse_command(payload: bytes | bytearray | memoryview) -> Command | None:
        try:
            decoded = bytes(payload).decode("utf-8")
            command = Command.model_validate_json(decoded)
        except (UnicodeDecodeError, ValueError):
            log.warning("basic_station.command_invalid_json")
            return None

        return command

    async def _publish_heartbeat(
        self,
        client: _Publisher,
        status: str,
        run_id: str | None = None,
        active_lanes: Sequence[_LaneActivity] | None = None,
    ) -> None:
        payload = Heartbeat(
            ts=datetime.now(UTC),
            status=status,
            currentRunId=run_id,
            activeLanes=[
                HeartbeatActiveLanesItem(
                    lane=activity.lane,
                    runId=activity.run_id,
                    dutId=activity.dut_id,
                    status=activity.status,
                    currentStage=activity.current_stage,
                    waitingReason=activity.waiting_reason,
                )
                for activity in active_lanes
            ] if active_lanes else None,
        ).model_dump_json(by_alias=True)
        await client.publish(f"{self._settings.topic_prefix}/heartbeat", payload=payload, qos=1)

    async def _serve_heartbeat_loop(
        self,
        client: _Publisher,
        get_state: Callable[[], tuple[str, str | None, Sequence[_LaneActivity]]],
        interval_s: float = 5.0,
    ) -> None:
        while True:
            status, run_id, active_lanes = get_state()
            await self._publish_heartbeat(client, status, run_id=run_id, active_lanes=active_lanes)
            await asyncio.sleep(interval_s)

    async def _publish_stage_update(
        self,
        client: _Publisher,
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

    async def _publish_result(self, client: _Publisher, result: TestResult, lane: str) -> None:
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

    async def _load_runtime_configuration(self) -> RuntimeConfiguration | None:
        try:
            config = await asyncio.to_thread(self._fetch_runtime_configuration)
        except (HTTPError, URLError, TimeoutError) as exc:
            log.warning("basic_station.configuration_pull_failed", error=str(exc))
            return None

        try:
            lanes, plans = parse_runtime_plans_from_yaml_text(config.yaml, self._settings.programmers)
            self._plans = {plan.lane: _RunPlan(plan.lane, plan.stages) for plan in plans}
            self._stages = list(next(iter(self._plans.values())).stages)
        except ConfigError as exc:
            log.warning("basic_station.configuration_invalid", revision=config.revision, error=str(exc))
            return config

        self._runtime_config_revision = config.revision
        log.info("basic_station.configuration_loaded", revision=config.revision, stages=len(self._stages))
        return config

    def _fetch_runtime_configuration(self) -> RuntimeConfiguration:
        url = f"{self._settings.api_base_url.rstrip('/')}/api/v1/stations/{self._settings.station_id}/configuration"
        headers = {"Accept": "application/json"}
        if self._settings.station_key:
            headers["X-Station-Key"] = self._settings.station_key

        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")

        payload = json.loads(body)
        return RuntimeConfiguration(
            revision=int(payload["revision"]),
            yaml=str(payload["runtimeYaml"]),
        )
