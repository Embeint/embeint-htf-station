from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

from embeint_htf_station.config import ConfigError, Settings, parse_stage_settings_from_yaml_text
from embeint_htf_station.messaging.batch_logger import BatchLogger
from embeint_htf_station.messaging.client import connect
from embeint_htf_station.stages import StageFactory, StageResult, create_stage, default_stage_factories

log = structlog.get_logger(__name__)


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


class StageScopedLogger:
    def __init__(self, logger: BatchLogger, stage_name: str) -> None:
        self._logger = logger
        self._stage_name = stage_name

    async def start(self) -> None:
        formatted = f"======={self._stage_name}======="
        print(formatted)
        await self._logger.log("info", formatted)

    async def log(self, level: str, msg: str) -> None:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        formatted = f"[{timestamp}][{self._stage_name}] - {msg}"
        print(formatted)
        await self._logger.log(level, formatted)


class BasicStation:
    """Minimal station used to validate the server-to-station pipeline."""

    def __init__(self, settings: Settings, stage_factories: Mapping[str, StageFactory] | None = None) -> None:
        self._settings = settings
        self._stages = list(settings.stages)
        self._runtime_config_revision: int | None = None
        self._stage_factories = default_stage_factories()
        if stage_factories:
            self._stage_factories.update(stage_factories)

    async def run_once(self, dut_id: str) -> TestResult:
        await self._load_runtime_configuration()
        async with connect(self._settings) as client:
            return await self._run_test(client, dut_id, run_id=None)

    async def serve_forever(self) -> None:
        await self._load_runtime_configuration()
        async with connect(self._settings) as client:
            await client.subscribe(f"{self._settings.topic_prefix}/cmd", qos=1)
            log.info("basic_station.subscribed", topic=f"{self._settings.topic_prefix}/cmd")

            current_run_id: str | None = None
            current_run_task: asyncio.Task[TestResult] | None = None
            heartbeat_task = asyncio.create_task(
                self._serve_heartbeat_loop(
                    client,
                    lambda: (
                        "running" if current_run_task and not current_run_task.done() else "idle",
                        current_run_id if current_run_task and not current_run_task.done() else None,
                    ),
                ),
            )

            try:
                async for message in client.messages:
                    command = self._parse_command(message.payload)
                    if command is None:
                        continue

                    payload = command.get("payload")
                    if not isinstance(payload, dict):
                        log.warning("basic_station.command_missing_payload")
                        continue

                    if command.get("kind") == "abort-run":
                        run_id = payload.get("runId")
                        if isinstance(run_id, str) and current_run_task and not current_run_task.done() and run_id == current_run_id:
                            log.info("basic_station.abort_requested", run_id=run_id)
                            current_run_task.cancel()
                        else:
                            log.warning("basic_station.abort_ignored", run_id=run_id, active_run_id=current_run_id)
                        continue

                    if command.get("kind") != "run-plan":
                        log.info("basic_station.command_ignored", kind=command.get("kind"))
                        continue

                    if current_run_task and not current_run_task.done():
                        log.warning("basic_station.run_ignored_busy", active_run_id=current_run_id)
                        continue

                    dut_id = payload.get("dutId")
                    if not isinstance(dut_id, str) or not dut_id.strip():
                        log.warning("basic_station.command_missing_dut_id")
                        continue

                    run_id = payload.get("runId")
                    current_run_id = run_id if isinstance(run_id, str) else None
                    current_run_task = asyncio.create_task(self._run_test(
                        client,
                        dut_id.strip(),
                        run_id=current_run_id,
                    ))
                    current_run_task.add_done_callback(self._log_run_task_result)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _run_test(self, client: _Publisher, dut_id: str, run_id: str | None) -> TestResult:
        started_at = datetime.now(UTC)
        logger = BatchLogger(client, f"{self._settings.topic_prefix}/log", run_id=run_id)
        await logger.start()
        try:
            await self._publish_heartbeat(client, "running", run_id=run_id)
            await logger.log("info", f"starting basic test for DUT {dut_id}")
            for index, stage in enumerate(self._stages):
                await self._publish_stage_update(client, run_id, index, stage.name, "pending")

            stages: list[StageResult] = []
            for index, stage_settings in enumerate(self._stages):
                await self._publish_stage_update(client, run_id, index, stage_settings.name, "running")
                stage_logger = StageScopedLogger(logger, stage_settings.name)
                await stage_logger.start()
                stage_started_at = datetime.now(UTC)
                try:
                    stage_result = await create_stage(stage_settings, self._stage_factories).run(stage_logger)
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
                    await self._publish_stage_update(client, run_id, index, stage_result.name, stage_result.outcome)
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
                    await self._publish_result(client, result)
                    log.info("basic_test.finished", dut_id=dut_id, outcome="aborted")
                    return result
                stages.append(stage_result)
                await self._publish_stage_update(client, run_id, index, stage_result.name, stage_result.outcome)

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
            await self._publish_result(client, result)
            log.info("basic_test.finished", dut_id=dut_id, outcome=outcome)
            return result
        finally:
            await logger.stop()
            await self._publish_heartbeat(client, "idle")

    @staticmethod
    def _log_run_task_result(task: asyncio.Task[TestResult]) -> None:
        if task.cancelled():
            log.warning("basic_station.run_task_cancelled")
            return
        exc = task.exception()
        if exc is not None:
            log.error("basic_station.run_task_failed", error=str(exc))

    @staticmethod
    def _parse_command(payload: bytes | bytearray | memoryview) -> dict[str, object] | None:
        try:
            decoded = bytes(payload).decode("utf-8")
            command = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("basic_station.command_invalid_json")
            return None

        return command if isinstance(command, dict) else None

    async def _publish_heartbeat(self, client: _Publisher, status: str, run_id: str | None = None) -> None:
        payload = json.dumps({"ts": datetime.now(UTC).isoformat(), "status": status, "currentRunId": run_id})
        await client.publish(f"{self._settings.topic_prefix}/heartbeat", payload=payload, qos=1)

    async def _serve_heartbeat_loop(
        self,
        client: _Publisher,
        get_state: Callable[[], tuple[str, str | None]],
        interval_s: float = 5.0,
    ) -> None:
        while True:
            status, run_id = get_state()
            await self._publish_heartbeat(client, status, run_id=run_id)
            await asyncio.sleep(interval_s)

    async def _publish_stage_update(
        self,
        client: _Publisher,
        run_id: str | None,
        index: int,
        name: str,
        status: str,
    ) -> None:
        payload = json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "runId": run_id,
                "index": index,
                "name": name,
                "status": status,
            },
        )
        await client.publish(f"{self._settings.topic_prefix}/stage", payload=payload, qos=1)

    async def _publish_result(self, client: _Publisher, result: TestResult) -> None:
        payload = json.dumps(
            {
                "ts": result.finished_at.isoformat(),
                "runId": result.run_id,
                "dutId": result.dut_id,
                "outcome": result.outcome,
                "configRevision": result.config_revision,
                "startedAt": result.started_at.isoformat(),
                "finishedAt": result.finished_at.isoformat(),
                "stages": [
                    {
                        "name": stage.name,
                        "outcome": stage.outcome,
                        "startedAt": stage.started_at.isoformat(),
                        "finishedAt": stage.finished_at.isoformat(),
                    }
                    for stage in result.stages
                ],
            },
        )
        await client.publish(f"{self._settings.topic_prefix}/result", payload=payload, qos=1)

    async def _load_runtime_configuration(self) -> RuntimeConfiguration | None:
        try:
            config = await asyncio.to_thread(self._fetch_runtime_configuration)
        except (HTTPError, URLError, TimeoutError) as exc:
            log.warning("basic_station.configuration_pull_failed", error=str(exc))
            return None

        try:
            self._stages = list(parse_stage_settings_from_yaml_text(config.yaml))
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
