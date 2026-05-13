from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult
from embeint_htf_station.stages.nrfutil import NrfutilError, _describe_programmer, _resolve_programmer
from embeint_htf_station.targets import HardwareIdTarget, target_device_settings


class HardwareIdError(RuntimeError):
    """Raised when a target hardware ID cannot be read."""

_MEM32_RE = re.compile(r"(?P<address>[0-9A-Fa-f]{8})\s*=\s*(?P<values>(?:[0-9A-Fa-f]{8}\s*)+)")


class HardwareIdStage:
    def __init__(self, settings: StageSettings, programmers: Mapping[str, ProgrammerSettings]) -> None:
        self._settings = settings
        self._programmers = programmers

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        try:
            programmer = _resolve_programmer(self._settings, self._programmers)
            if programmer is None:
                raise HardwareIdError("hardware_id stage requires a named programmer")
            await logger.log("info", _describe_programmer(programmer))

            target = _target_for(programmer, self._settings)
            await logger.log(
                "info",
                f"reading hardware ID from target={programmer.target_device}, address=0x{target.address:08X}, words={target.words}",
            )
            hardware_id = await _read_hardware_id(programmer, target, logger)
            context.set_output(self._settings.name, "hardware_id", hardware_id)
            context.set_output(self._settings.name, "target_device", programmer.target_device or "")
            await logger.log("info", f"hardware ID: {hardware_id}")
            outcome = "passed"
            await logger.log("info", "stage passed")
        except (HardwareIdError, NrfutilError, OSError) as exc:
            outcome = "failed"
            await logger.log("error", str(exc))

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def _target_for(programmer: ProgrammerSettings, settings: StageSettings) -> HardwareIdTarget:
    address = _optional_int(settings.hardware_id_address)
    words = settings.hardware_id_words
    if address is not None and words is not None:
        return HardwareIdTarget(address=address, words=words)

    target = target_device_settings(programmer.target_device)
    if target is None or target.hardware_id is None:
        raise HardwareIdError(
            f"no hardware ID reader configured for target_device={programmer.target_device!r}; "
            "set hardware_id_address and hardware_id_words on the stage",
        )
    return HardwareIdTarget(address=address or target.hardware_id.address, words=words or target.hardware_id.words)


async def _read_hardware_id(programmer: ProgrammerSettings, target: HardwareIdTarget, logger: StageLogger) -> str:
    script_path = _write_jlink_script(target)
    command = _jlink_command(programmer, script_path)
    await logger.log("info", f"running: {' '.join(command)}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await process.communicate()
    script_path.unlink(missing_ok=True)
    output = stdout.decode("utf-8", errors="replace")
    error_output = stderr.decode("utf-8", errors="replace")
    for line in output.splitlines():
        if line.strip():
            await logger.log("info", line.strip())
    for line in error_output.splitlines():
        if line.strip():
            await logger.log("warning", line.strip())
    if process.returncode != 0:
        raise HardwareIdError(f"JLinkExe exited with status {process.returncode}")
    return _parse_mem32_hardware_id(output, target.words)


def _write_jlink_script(target: HardwareIdTarget) -> Path:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="ascii", suffix=".jlink", delete=False)
    with handle:
        handle.write(f"mem32 0x{target.address:08X},{target.words}\n")
        handle.write("q\n")
    return Path(handle.name)


def _jlink_command(programmer: ProgrammerSettings, script_path: Path) -> tuple[str, ...]:
    if not programmer.target_device:
        raise HardwareIdError("hardware_id stage requires programmer.target_device")
    command = [
        "JLinkExe",
        "-device",
        programmer.target_device,
        "-if",
        "SWD",
        "-speed",
        "4000",
        "-autoconnect",
        "1",
        "-CommanderScript",
        str(script_path),
    ]
    if programmer.serial_number is not None:
        command.extend(("-USB", str(programmer.serial_number)))
    return tuple(command)


def _parse_mem32_hardware_id(output: str, expected_words: int) -> str:
    words: list[int] = []
    for match in _MEM32_RE.finditer(output):
        for value in match.group("values").split():
            words.append(int(value, 16))
            if len(words) == expected_words:
                break
        if len(words) == expected_words:
            break
    if len(words) != expected_words:
        raise HardwareIdError("could not parse hardware ID from JLinkExe mem32 output")

    hardware_id = 0
    for index, word in enumerate(words):
        hardware_id |= word << (32 * index)
    return f"{hardware_id:0{expected_words * 8}x}"


def _optional_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    return int(str(value), 0)
