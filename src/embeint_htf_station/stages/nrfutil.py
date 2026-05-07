from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.firmware import FirmwareCache, FirmwareError
from embeint_htf_station.stages.base import StageLogger, StageResult


class NrfutilError(RuntimeError):
    """Raised when an nrfutil stage is not configured correctly."""


class NrfutilDeviceRecoverStage:
    def __init__(self, settings: StageSettings, programmers: Mapping[str, ProgrammerSettings]) -> None:
        self._settings = settings
        self._programmers = programmers

    async def run(self, logger: StageLogger) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        try:
            programmer = _resolve_programmer(self._settings, self._programmers)
            await logger.log("info", _describe_programmer(programmer))
            await _run_command(_nrfutil_device_command(("recover",), programmer), logger)
            outcome = "passed"
            await logger.log("info", "stage passed")
        except (NrfutilError, OSError) as exc:
            outcome = "failed"
            await logger.log("error", str(exc))

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


class NrfutilDeviceResetStage:
    def __init__(self, settings: StageSettings, programmers: Mapping[str, ProgrammerSettings]) -> None:
        self._settings = settings
        self._programmers = programmers

    async def run(self, logger: StageLogger) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        try:
            programmer = _resolve_programmer(self._settings, self._programmers)
            await logger.log("info", _describe_programmer(programmer))
            await _run_command(_nrfutil_device_command(("reset",), programmer), logger)
            outcome = "passed"
            await logger.log("info", "stage passed")
        except (NrfutilError, OSError) as exc:
            outcome = "failed"
            await logger.log("error", str(exc))

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


class FirmwareFlashStage:
    def __init__(
        self,
        settings: StageSettings,
        programmers: Mapping[str, ProgrammerSettings],
        firmware_cache: FirmwareCache,
    ) -> None:
        self._settings = settings
        self._programmers = programmers
        self._firmware_cache = firmware_cache

    async def run(self, logger: StageLogger) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        try:
            programmer = _resolve_programmer(self._settings, self._programmers)
            firmware_id = _required_value(self._settings.firmware_id, "firmware_id")
            path_in_archive = _required_value(self._settings.path, "path")
            await logger.log("info", _describe_programmer(programmer))
            await logger.log(
                "info",
                f"resolving firmware {firmware_id} ({self._settings.firmware_version})",
            )
            firmware_path = self._firmware_cache.get_file(
                firmware_id,
                self._settings.firmware_version,
                path_in_archive,
            )
            await logger.log("info", f"using firmware file {firmware_path}")
            await _run_command(
                _nrfutil_device_command(("program", "--firmware", str(firmware_path)), programmer),
                logger,
            )
            outcome = "passed"
            await logger.log("info", "stage passed")
        except (FirmwareError, NrfutilError, OSError) as exc:
            outcome = "failed"
            await logger.log("error", str(exc))

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def _resolve_programmer(
    settings: StageSettings,
    programmers: Mapping[str, ProgrammerSettings],
) -> ProgrammerSettings | None:
    if settings.programmer is None:
        return None

    programmer = programmers.get(settings.programmer)
    if programmer is None:
        raise NrfutilError(f"unknown programmer: {settings.programmer}")
    if programmer.kind != "jlink":
        raise NrfutilError(f"unsupported programmer kind for nrfutil: {programmer.kind}")
    return programmer


def _describe_programmer(programmer: ProgrammerSettings | None) -> str:
    if programmer is None:
        return "using default nrfutil device selection"

    parts = [f"using programmer {programmer.name}", f"kind={programmer.kind}"]
    if programmer.serial_number is not None:
        parts.append(f"serial={programmer.serial_number}")
    if programmer.target_device:
        parts.append(f"target={programmer.target_device}")
    return ", ".join(parts)


def _nrfutil_device_command(
    args: Sequence[str],
    programmer: ProgrammerSettings | None,
) -> tuple[str, ...]:
    command = ["nrfutil", "device", *args]
    if programmer is not None and programmer.serial_number is not None:
        command.extend(("--serial-number", str(programmer.serial_number)))
    return tuple(command)


def _required_value(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise NrfutilError(f"firmware flash stage requires {field_name}")
    return value.strip()


async def _run_command(args: Sequence[str], logger: StageLogger) -> None:
    await logger.log("info", f"running: {' '.join(args)}")
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        await _stream_process(process, logger)
    except asyncio.CancelledError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise
    exit_code = await process.wait()
    if exit_code != 0:
        raise NrfutilError(f"nrfutil exited with status {exit_code}")


async def _stream_process(process: asyncio.subprocess.Process, logger: StageLogger) -> None:
    async def stream_output(stream: asyncio.StreamReader | None, level: str) -> None:
        if stream is None:
            return
        while line := await stream.readline():
            await logger.log(level, line.decode("utf-8", errors="replace").rstrip())

    await asyncio.gather(
        stream_output(process.stdout, "info"),
        stream_output(process.stderr, "warning"),
    )
