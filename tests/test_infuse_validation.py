from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages import StageContext
from embeint_htf_station.stages.infuse_validation import (
    InfuseValidationError,
    InfuseValidationStage,
    InfuseValidationState,
    _validate_infuse_state,
    parse_infuse_line,
)


class FakeTransport:
    def __init__(self, lines: list[str], *, delay: float = 0) -> None:
        self._lines = lines
        self._delay = delay
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    async def lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield line


@dataclass
class Logger:
    entries: list[tuple[str, str]] = field(default_factory=list)

    async def log(self, level: str, message: str) -> None:
        self.entries.append((level, message))


def test_parse_infuse_validation_output_from_sample() -> None:
    state = InfuseValidationState(required_tests={"BT", "MODEM", "DISK", "FLASH", "LED", "IMU"})
    sample = Path("samples/basic-validation/example-ouput.txt")

    for line in sample.read_text(encoding="utf-8").splitlines():
        parse_infuse_line(line, state)

    assert state.infuse_id == "0xfffff27435b4f6f7"
    assert state.success_passed == 9
    assert state.success_total == 9
    assert {"BT", "MODEM", "DISK", "FLASH", "LED", "IMU", "PWR", "ENV"}.issubset(state.passed_tests)
    assert state.values["FLASH"]["PAGE_SIZE"] == "65536"
    assert state.values["PWR"]["SOC"] == "100"
    _validate_infuse_state(state, StageSettings(
        name="Validation",
        kind="infuse_validation",
        tests=("BT", "MODEM", "DISK", "FLASH", "LED", "IMU"),
        number_of_tests=10,
    ))


def test_validate_infuse_state_fails_when_required_test_missing() -> None:
    state = InfuseValidationState(required_tests={"BT", "MODEM"})
    parse_infuse_line("000077:BT:PASS:PASSED", state)
    parse_infuse_line("039501:SYS:SUCCESS:Complete with 1/1 passed", state)

    with pytest.raises(InfuseValidationError, match="MODEM"):
        _validate_infuse_state(state, StageSettings(
            name="Validation",
            kind="infuse_validation",
            tests=("BT", "MODEM"),
        ))


def test_infuse_validation_rtt_commands_use_jlink_telnet_transport() -> None:
    stage = InfuseValidationStage(
        StageSettings(
            name="Validation",
            kind="infuse_validation_rtt",
            programmer="jlink_1",
            rtt_telnet_port=19021,
        ),
        {
            "jlink_1": ProgrammerSettings(
                name="jlink_1",
                kind="jlink",
                serial_number=823000667,
                target_device="nrf54l15_m33",
                rtt_telnet_port=19022,
            ),
        },
    )

    assert stage._jlink_command() == (
        "JLinkExe",
        "-device",
        "nrf54l15_m33",
        "-if",
        "SWD",
        "-speed",
        "4000",
        "-autoconnect",
        "1",
        "-RTTTelnetPort",
        "19022",
        "-USB",
        "823000667",
    )
    assert stage._rtt_client_command() == ("JLinkRTTClientExe", "-rtttelnetport", "19022")


def test_infuse_sys_error_completion_reports_pass_count() -> None:
    state = InfuseValidationState(required_tests={"BT"})
    parse_infuse_line("000077:BT:PASS:PASSED", state)
    parse_infuse_line("039501:SYS:ERROR:Complete with 8/9 passed", state)

    assert state.complete is True
    assert state.success_passed == 8
    assert state.success_total == 9
    assert "SYS" not in state.failed_tests
    with pytest.raises(InfuseValidationError, match="8/9"):
        _validate_infuse_state(state, StageSettings(
            name="Validation",
            kind="infuse_validation",
            tests=("BT",),
        ))


def test_infuse_sys_pass_completion_reports_pass_count() -> None:
    state = InfuseValidationState(required_tests={"BT"})
    parse_infuse_line("000077:BT:PASS:PASSED", state)
    parse_infuse_line("039640:SYS:PASS:Complete with 9/9 passed", state)

    assert state.complete is True
    assert state.success_passed == 9
    assert state.success_total == 9
    assert "SYS" not in state.passed_tests
    _validate_infuse_state(state, StageSettings(
        name="Validation",
        kind="infuse_validation",
        tests=("BT",),
    ))


async def test_validation_stage_passes_complete_transport_output_and_cleans_up() -> None:
    transport = FakeTransport([
        "000077:BT:PASS:PASSED",
        "000078:SYS:SUCCESS:Complete with 1/1 passed",
    ])
    stage = InfuseValidationStage(
        StageSettings(name="Validation", tests=("BT",), number_of_tests=1),
        {},
        transport=transport,
    )

    result = await stage.run(Logger(), StageContext("DUT"))

    assert result.outcome == "passed"
    assert transport.exited is True


async def test_validation_stage_fails_malformed_output_and_cleans_up() -> None:
    transport = FakeTransport(["not an infuse validation line"])
    stage = InfuseValidationStage(StageSettings(name="Validation"), {}, transport=transport)

    result = await stage.run(Logger(), StageContext("DUT"))

    assert result.outcome == "failed"
    assert transport.exited is True


async def test_validation_stage_times_out_and_cleans_up() -> None:
    transport = FakeTransport(["not complete"], delay=0.05)
    stage = InfuseValidationStage(
        StageSettings(name="Validation", test_timeout_seconds=0.001),
        {},
        transport=transport,
    )

    result = await stage.run(Logger(), StageContext("DUT"))

    assert result.outcome == "failed"
    assert transport.exited is True
