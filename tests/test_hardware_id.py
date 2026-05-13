from __future__ import annotations

import pytest

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages.hardware_id import HardwareIdError, _jlink_command, _parse_mem32_hardware_id, _target_for


def test_hardware_id_parses_jlink_mem32_output_as_little_endian_words() -> None:
    output = """
J-Link>mem32 0x00FFC304,2
00FFC304 = 44332211 88776655
"""

    assert _parse_mem32_hardware_id(output, 2) == "8877665544332211"


def test_hardware_id_uses_target_device_defaults() -> None:
    target = _target_for(
        ProgrammerSettings(name="jlink_1", kind="jlink", target_device="nrf54l15_m33"),
        StageSettings(name="Get Hardware ID", kind="hardware_id"),
    )

    assert target.address == 0x00FFC304
    assert target.words == 2


def test_hardware_id_requires_known_target_or_explicit_address() -> None:
    with pytest.raises(HardwareIdError, match="hardware_id_address"):
        _target_for(
            ProgrammerSettings(name="jlink_1", kind="jlink", target_device="unknown"),
            StageSettings(name="Get Hardware ID", kind="hardware_id"),
        )


def test_hardware_id_jlink_command_uses_programmer_target_and_serial(tmp_path) -> None:
    command = _jlink_command(
        ProgrammerSettings(
            name="jlink_1",
            kind="jlink",
            serial_number=823000667,
            target_device="nrf54l15_m33",
        ),
        tmp_path / "read.jlink",
    )

    assert command == (
        "JLinkExe",
        "-device",
        "nrf54l15_m33",
        "-if",
        "SWD",
        "-speed",
        "4000",
        "-autoconnect",
        "1",
        "-CommanderScript",
        str(tmp_path / "read.jlink"),
        "-USB",
        "823000667",
    )
