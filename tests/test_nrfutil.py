from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages import StageContext
from embeint_htf_station.stages.nrfutil import (
    FirmwareFlashStage,
    NrfutilDeviceRecoverStage,
    NrfutilDeviceResetStage,
    NrfutilError,
)


@dataclass
class Logger:
    entries: list[tuple[str, str]] = field(default_factory=list)

    async def log(self, level: str, msg: str) -> None:
        self.entries.append((level, msg))


class Firmware:
    def get_file(self, firmware_id: str, version: str, path: str) -> Path:
        assert (firmware_id, version, path) == ("firmware-1", "latest", "app.hex")
        return Path("/cache/app.hex")


@pytest.mark.parametrize("stage_type,operation", [
    (NrfutilDeviceRecoverStage, "recover"),
    (NrfutilDeviceResetStage, "reset"),
])
async def test_device_stage_reports_process_failure(stage_type: type, operation: str) -> None:
    commands: list[tuple[str, ...]] = []

    async def fail(command, _logger) -> None:
        commands.append(tuple(command))
        raise NrfutilError("nrfutil exited with status 2")

    stage = stage_type(
        StageSettings(name=operation, programmer="left"),
        {"left": ProgrammerSettings(name="left", kind="jlink", serial_number="123")},
        fail,
    )
    logger = Logger()

    result = await stage.run(logger, StageContext("DUT"))

    assert result.outcome == "failed"
    assert commands == [("nrfutil", "device", operation, "--serial-number", "123")]
    assert logger.entries[-1] == ("error", "nrfutil exited with status 2")


async def test_firmware_flash_uses_resolved_file_and_injected_runner() -> None:
    commands: list[tuple[str, ...]] = []

    async def run(command, _logger) -> None:
        commands.append(tuple(command))

    stage = FirmwareFlashStage(
        StageSettings(name="flash", firmware_id="firmware-1", path="app.hex"),
        {},
        Firmware(),  # type: ignore[arg-type]
        run,
    )

    result = await stage.run(Logger(), StageContext("DUT"))

    assert result.outcome == "passed"
    assert commands == [("nrfutil", "device", "program", "--firmware", "/cache/app.hex")]


async def test_firmware_flash_rejects_missing_archive_path_without_running_process() -> None:
    async def unexpected(_command, _logger) -> None:
        raise AssertionError("runner must not be called")

    stage = FirmwareFlashStage(
        StageSettings(name="flash", firmware_id="firmware-1"),
        {},
        Firmware(),  # type: ignore[arg-type]
        unexpected,
    )
    logger = Logger()

    result = await stage.run(logger, StageContext("DUT"))

    assert result.outcome == "failed"
    assert logger.entries[-1] == ("error", "firmware flash stage requires path")


async def test_nrfutil_stage_rejects_wrong_programmer_kind() -> None:
    stage = NrfutilDeviceRecoverStage(
        StageSettings(name="recover", programmer="left"),
        {"left": ProgrammerSettings(name="left", kind="simulated")},
    )
    logger = Logger()

    result = await stage.run(logger, StageContext("DUT"))

    assert result.outcome == "failed"
    assert logger.entries[-1] == ("error", "unsupported programmer kind for nrfutil: simulated")
