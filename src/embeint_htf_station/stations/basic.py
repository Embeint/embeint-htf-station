from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

import structlog

from embeint_htf_station.config import (
    ConfigError, Settings, StageSettings,
    parse_runtime_plans_from_yaml_text,
)
from embeint_htf_station.contracts.mqtt import Command, Heartbeat, HeartbeatActiveLanesItem
from embeint_htf_station.firmware import FirmwareCache
from embeint_htf_station.messaging.client import connect
from embeint_htf_station.stages import StageFactory, default_stage_factories
from embeint_htf_station.stages.hardware_id import HardwareIdStage
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage
from embeint_htf_station.stages.infuse_validation import InfuseValidationHook, InfuseValidationStage
from embeint_htf_station.stages.nrfutil import FirmwareFlashStage, NrfutilDeviceRecoverStage, NrfutilDeviceResetStage
from embeint_htf_station.stages.simulated_programmer import SimulatedProgrammerStage
from embeint_htf_station.stations.scheduler import (
    ActiveRun as _ActiveRun,
    LaneActivity as _LaneActivity,
    LaneScheduler,
    QueuedRun as _QueuedRun,
    RunPlan as _RunPlan,
    abort_queued_run,
    active_run_state,
    complete_run_futures,
    find_active_run_by_id,
    run_lane_worker,
)
from embeint_htf_station.stations.command_receipts import CommandReceiptStore
from embeint_htf_station.stations.stage_runner import StageRunner, StageScopedLogger as _StageScopedLogger, TestResult

log = structlog.get_logger(__name__)

StageScopedLogger = _StageScopedLogger

class _Publisher(Protocol):
    async def publish(self, topic: str, payload: str, qos: int = 0) -> None: ...


@dataclass(frozen=True)
class RuntimeConfiguration:
    revision: int
    yaml: str


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
        self._command_receipts = CommandReceiptStore(settings)
        self._stage_factories = default_stage_factories()
        self._stage_factories.update({
            "nrfutil_device_recover": lambda stage: NrfutilDeviceRecoverStage(stage, self._programmers),
            "nrfutil_reset": lambda stage: NrfutilDeviceResetStage(stage, self._programmers),
            "firmware_flash": lambda stage: FirmwareFlashStage(stage, self._programmers, self._firmware_cache),
            "hardware_id": lambda stage: HardwareIdStage(stage, self._programmers),
            "infuse_validation": lambda stage: InfuseValidationStage(stage, self._programmers, infuse_validation_hooks),
            "infuse_validation_rtt": lambda stage: InfuseValidationStage(stage, self._programmers, infuse_validation_hooks),
            "infuse_provisioning": lambda stage: InfuseProvisioningStage(stage, self._programmers, self._settings),
            "simulated_programmer": lambda stage: SimulatedProgrammerStage(stage, self._programmers),
        })
        if stage_factories:
            self._stage_factories.update(stage_factories)
        self._stage_runner = StageRunner(
            settings,
            self._stage_factories,
            self._stage_locks,
            lambda: self._runtime_config_revision,
            self._publish_heartbeat,
        )

    async def run_once(self, dut_id: str) -> TestResult:
        await self._load_runtime_configuration()
        plan = self._default_run_plan()
        async with connect(self._settings) as client:
            return await self._run_test(client, dut_id, run_id=None, stages=plan.stages, lane=plan.lane)

    async def serve_forever(self) -> None:
        await self._load_runtime_configuration()
        async with connect(self._settings) as client:
            await self._recover_interrupted_commands(client)
            await client.subscribe(f"{self._settings.topic_prefix}/cmd", qos=1)
            log.info("basic_station.subscribed", topic=f"{self._settings.topic_prefix}/cmd")

            scheduler = LaneScheduler(
                client,
                self._plans,
                self._run_test,
                self._publish_aborted_without_stages,
                self._complete_queued_command,
            )
            scheduler.start()
            heartbeat_task = asyncio.create_task(
                self._serve_heartbeat_loop(
                    client,
                    scheduler.state,
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
                    if not self._command_receipts.claim(command):
                        log.info("basic_station.command_duplicate", command_id=str(command.id))
                        continue

                    if command.kind == "abort-run":
                        run_id = payload.get("runId")
                        if scheduler.find_active(run_id) is not None:
                            log.info("basic_station.abort_requested", run_id=run_id)
                        await scheduler.abort(run_id)
                        self._command_receipts.complete(command.id)
                        continue

                    if command.kind == "run-batch":
                        queued = await self._enqueue_batch(client, payload, scheduler.queues, command.id)
                        if queued == 0:
                            self._command_receipts.complete(command.id)
                        continue

                    if command.kind != "run-plan":
                        log.info("basic_station.command_ignored", kind=command.kind)
                        self._command_receipts.complete(command.id)
                        continue

                    dut_id = payload.get("dutId")
                    if not isinstance(dut_id, str) or not dut_id.strip():
                        log.warning("basic_station.command_missing_dut_id")
                        if isinstance(payload.get("runId"), str):
                            await self._publish_terminal_error(client, payload["runId"], "unknown", "command missing DUT ID")
                            self._command_receipts.complete_run(command.id, payload["runId"])
                        else:
                            self._command_receipts.complete(command.id)
                        continue

                    plan = self._run_plan_from_payload(payload)
                    if plan is None:
                        if isinstance(payload.get("runId"), str):
                            await self._publish_terminal_error(client, payload["runId"], dut_id.strip(), "invalid or stale lane command")
                            self._command_receipts.complete_run(command.id, payload["runId"])
                        else:
                            self._command_receipts.complete(command.id)
                        continue
                    run_id = payload.get("runId")
                    if not isinstance(run_id, str) or not run_id.strip():
                        log.warning("basic_station.command_missing_run_id")
                        self._command_receipts.complete(command.id)
                        continue
                    await scheduler.enqueue(_QueuedRun(run_id, dut_id.strip(), plan, {}, command.id))
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                await scheduler.stop()

    async def _recover_interrupted_commands(self, client: _Publisher) -> None:
        for receipt in self._command_receipts.incomplete():
            log.warning(
                "basic_station.command_interrupted",
                command_id=str(receipt.command_id),
                kind=receipt.kind,
                runs=len(receipt.runs),
            )
            for run in receipt.runs:
                await self._publish_terminal_error(
                    client,
                    run.run_id,
                    run.dut_id,
                    "station restarted before command completion; hardware execution state is unknown",
                    run.lane,
                )
            self._command_receipts.complete(receipt.command_id)

    def _complete_queued_command(self, queued: _QueuedRun) -> None:
        if queued.command_id is not None and queued.run_id is not None:
            self._command_receipts.complete_run(queued.command_id, queued.run_id)

    async def _enqueue_batch(
        self,
        client: _Publisher,
        payload: dict[str, object],
        lane_queues: Mapping[str, asyncio.Queue[_QueuedRun]],
        command_id: UUID | None = None,
    ) -> int:
        entries = payload.get("runs")
        if not isinstance(entries, list):
            log.warning("basic_station.batch_invalid", error="runs is required")
            return 0
        command_revision = payload.get("configRevision")
        if command_revision != self._runtime_config_revision:
            log.warning(
                "basic_station.batch_revision_mismatch",
                command_revision=command_revision,
                loaded_revision=self._runtime_config_revision,
            )
            for item in entries:
                if not isinstance(item, dict):
                    continue
                run_id, lane, dut_id = item.get("runId"), item.get("lane"), item.get("dutId")
                if isinstance(run_id, str) and isinstance(dut_id, str):
                    await self._publish_terminal_error(
                        client,
                        run_id,
                        dut_id,
                        f"configuration revision mismatch: command={command_revision}, loaded={self._runtime_config_revision}",
                        lane if isinstance(lane, str) else "default",
                    )
                    if command_id is not None:
                        self._command_receipts.complete_run(command_id, run_id)
            return 0
        futures: dict[tuple[str, str], asyncio.Future[str]] = {}
        parsed: list[tuple[str, str, str]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            run_id, lane, dut_id = item.get("runId"), item.get("lane"), item.get("dutId")
            if not all(isinstance(value, str) and value.strip() for value in (run_id, lane, dut_id)) or lane not in self._plans:
                if isinstance(run_id, str) and isinstance(dut_id, str):
                    await self._publish_terminal_error(client, run_id, dut_id, "invalid batch lane or DUT")
                    if command_id is not None:
                        self._command_receipts.complete_run(command_id, run_id)
                continue
            if (lane, "__run__") in futures:
                await self._publish_terminal_error(client, run_id, dut_id, "duplicate lane in batch")
                if command_id is not None:
                    self._command_receipts.complete_run(command_id, run_id)
                continue
            futures[(lane, "__run__")] = asyncio.get_running_loop().create_future()
            for stage in self._plans[lane].stages:
                futures[(lane, stage.name)] = asyncio.get_running_loop().create_future()
            parsed.append((run_id, lane, dut_id))
        for run_id, lane, dut_id in parsed:
            await lane_queues[lane].put(_QueuedRun(run_id, dut_id, self._plans[lane], futures, command_id))
        return len(parsed)

    async def _lane_worker(
        self,
        client: _Publisher,
        lane: str,
        queue: asyncio.Queue[_QueuedRun],
        active_runs: dict[str, _ActiveRun],
        active_lanes: dict[str, _LaneActivity],
    ) -> None:
        await run_lane_worker(
            client,
            lane,
            queue,
            active_runs,
            active_lanes,
            self._run_test,
            self._publish_aborted_without_stages,
        )

    async def _abort_queued_run(self, client: _Publisher, queues: Mapping[str, asyncio.Queue[_QueuedRun]], run_id: object) -> None:
        await abort_queued_run(client, queues, run_id, self._publish_aborted_without_stages)

    async def _publish_aborted_without_stages(
        self, client: _Publisher, run_id: str | None, dut_id: str, lane: str,
    ) -> None:
        await self._stage_runner.publish_aborted(client, run_id, dut_id, lane)

    async def _publish_terminal_error(
        self, client: _Publisher, run_id: str, dut_id: str, reason: str, lane: str = "default",
    ) -> None:
        await self._stage_runner.publish_terminal_error(client, run_id, dut_id, reason, lane)

    async def _run_test(
        self,
        client: _Publisher,
        dut_id: str,
        run_id: str | None,
        *,
        stages: Sequence[StageSettings] | None = None,
        lane: str = "default",
        publish_idle_on_finish: bool = True,
        batch_results: Mapping[tuple[str, str], asyncio.Future[str]] | None = None,
        activity: _LaneActivity | None = None,
    ) -> TestResult:
        run_stages = tuple(stages) if stages is not None else tuple(self._stages)
        return await self._stage_runner.run(
            client,
            dut_id,
            run_id,
            stages=run_stages,
            lane=lane,
            publish_idle_on_finish=publish_idle_on_finish,
            batch_results=batch_results,
            activity=activity,
        )

    def _default_run_plan(self) -> _RunPlan:
        if len(self._plans) == 1:
            return next(iter(self._plans.values()))
        if "default" in self._plans:
            return self._plans["default"]
        raise ConfigError("multiple station lanes are configured; run command requires a lane")

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
        active_runs: Mapping[str, _ActiveRun],
        active_lanes: Mapping[str, _LaneActivity],
    ) -> tuple[str, str | None, tuple[_LaneActivity, ...]]:
        return active_run_state(active_runs, active_lanes)

    @staticmethod
    def _find_active_run_by_id(
        active_runs: Mapping[str, _ActiveRun],
        run_id: object,
    ) -> asyncio.Task[object] | None:
        return find_active_run_by_id(active_runs, run_id)

    @staticmethod
    def _complete_run_futures(queued: _QueuedRun, outcome: str) -> None:
        complete_run_futures(queued, outcome)

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
                for activity in active_lanes or ()
            ],
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
        await self._stage_runner.publish_stage_update(client, lane, run_id, index, name, status)

    async def _publish_result(self, client: _Publisher, result: TestResult, lane: str) -> None:
        await self._stage_runner.publish_result(client, result, lane)

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
