from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from embeint_htf_station.config import ProgrammerSettings, Settings, StageSettings, UicrWriteSettings
from embeint_htf_station.stages.base import StageContext
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage, UicrWrite, _intel_hex


@dataclass
class FakeLogger:
    entries: list[tuple[str, str]] = field(default_factory=list)

    async def log(self, level: str, msg: str) -> None:
        self.entries.append((level, msg))


def test_infuse_provisioning_generates_little_endian_uicr_hex() -> None:
    assert _intel_hex((
        UicrWrite(name="infuse_id", address=0x00FF8100, value=0x12345678, width_bits=32, byte_order="little"),
    )) == "\n".join((
        ":0200000400FFFB",
        ":048100007856341267",
        ":00000001FF",
        "",
    ))


async def test_infuse_provisioning_stage_programs_generated_hex(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    async def fake_run_command(args, logger) -> None:
        commands.append(tuple(args))

    stage = InfuseProvisioningStage(
        StageSettings(
            name="Device Provisioning",
            kind="infuse_provisioning",
            programmer="jlink_1",
            board_pool="kudu",
            uicr=(
                UicrWriteSettings(
                    name="infuse_id",
                    value="infuse_id",
                    width_bits=64,
                    byte_order="little",
                ),
            ),
        ),
        {
            "jlink_1": ProgrammerSettings(
                name="jlink_1",
                kind="jlink",
                serial_number=823000667,
                target_device="nrf54l15_m33",
                board="kudu",
            ),
        },
        Settings(org_id="org-1", station_id="station-1", firmware_cache_dir=str(tmp_path / "firmware")),
        run_command=fake_run_command,
    )
    stage._resolve_infuse_values = lambda programmer, hardware_id: {"infuse_id": "0xFFFFF27435B4F6F7"}  # type: ignore[method-assign]
    logger = FakeLogger()
    context = StageContext(dut_id="0xFFFFF27435B4F6F7")
    context.set_output("Get Hardware ID", "hardware_id", "0011223344556677")

    result = await stage.run(logger, context)

    assert result.outcome == "passed"
    assert context.get_output_value("provisioning.infuse_id") == "0xFFFFF27435B4F6F7"
    assert len(commands) == 1
    assert commands[0][:4] == ("nrfutil", "device", "program", "--firmware")
    hex_path = Path(commands[0][4])
    assert hex_path.exists()
    assert commands[0][-2:] == ("--serial-number", "823000667")
    assert ":08D50000F7F6B43574F2FFFF" in hex_path.read_text(encoding="ascii")
