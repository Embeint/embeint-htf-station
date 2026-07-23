from __future__ import annotations

from dataclasses import dataclass, field

from embeint_htf_station.config import LanePlanSettings, LaneSettings, Settings, StageSettings
from embeint_htf_station.stages import StageContext, create_stage
from embeint_htf_station.stages.hardware_id import HardwareIdStage
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage
from embeint_htf_station.stages.infuse_validation import InfuseValidationStage
from embeint_htf_station.stages.print_stage import PrintStage
from embeint_htf_station.stations.basic import BasicStation


@dataclass
class FakeLogger:
    entries: list[tuple[str, str]] = field(default_factory=list)

    async def log(self, level: str, msg: str) -> None:
        self.entries.append((level, msg))


def test_stage_context_tracks_typed_outputs() -> None:
    context = StageContext(dut_id="DUT-001", run_id="run-1")

    context.set_output("Get Hardware ID", "hardware_id", "0011223344556677")

    output = context.get_output("HARDWARE_ID")
    assert output is not None
    assert output.stage_name == "Get Hardware ID"
    assert output.value == "0011223344556677"
    assert context.get_output_value("hardware_id") == "0011223344556677"
    assert context.output_values["hardware_id"] == "0011223344556677"


async def test_print_stage_runs_from_settings(capsys) -> None:
    stage = PrintStage(StageSettings(name="print testing", message="testing", wait_seconds=0))
    logger = FakeLogger()

    result = await stage.run(logger, StageContext(dut_id="DUT-001"))

    assert result.name == "print testing"
    assert result.outcome == "passed"
    assert logger.entries == [
        ("info", "stage started"),
        ("info", "testing"),
        ("info", "stage passed"),
    ]
    assert capsys.readouterr().out == ""


async def test_create_stage_returns_unsupported_stage_for_unknown_kind() -> None:
    stage = create_stage(StageSettings(name="custom stage", kind="missing"))
    logger = FakeLogger()

    result = await stage.run(logger, StageContext(dut_id="DUT-001"))

    assert result.name == "custom stage"
    assert result.outcome == "failed"
    assert logger.entries == [("error", "unsupported stage kind: missing")]


def test_basic_station_registers_infuse_validation_rtt_alias() -> None:
    station = BasicStation(Settings(org_id="org-1", station_id="station-1"))
    settings = StageSettings(name="Validation", kind="infuse_validation_rtt")

    assert isinstance(create_stage(settings, station._stage_factories), InfuseValidationStage)


def test_basic_station_registers_infuse_provisioning_stage() -> None:
    station = BasicStation(Settings(org_id="org-1", station_id="station-1"))
    settings = StageSettings(name="Device Provisioning", kind="infuse_provisioning")

    assert isinstance(create_stage(settings, station._stage_factories), InfuseProvisioningStage)


def test_basic_station_registers_hardware_id_stage() -> None:
    station = BasicStation(Settings(org_id="org-1", station_id="station-1"))
    settings = StageSettings(name="Get Hardware ID", kind="hardware_id")

    assert isinstance(create_stage(settings, station._stage_factories), HardwareIdStage)


def test_basic_station_uses_default_plan_for_single_lane_command() -> None:
    station = BasicStation(Settings(
        org_id="org-1",
        station_id="station-1",
        lanes=(LaneSettings(name="left", programmer="jlink_1"),),
        plans=(LanePlanSettings(
            lane="left",
            stages=(StageSettings(name="Left Smoke", kind="print", programmer="jlink_1"),),
        ),),
    ))

    plan = station._run_plan_from_payload({})

    assert plan is not None
    assert plan.lane == "left"
    assert plan.stages[0].name == "Left Smoke"


def test_basic_station_requires_lane_for_multi_lane_command() -> None:
    station = BasicStation(Settings(
        org_id="org-1",
        station_id="station-1",
        lanes=(
            LaneSettings(name="left", programmer="jlink_1"),
            LaneSettings(name="right", programmer="jlink_2"),
        ),
        plans=(
            LanePlanSettings(lane="left", stages=(StageSettings(name="Left Smoke", kind="print"),)),
            LanePlanSettings(lane="right", stages=(StageSettings(name="Right Smoke", kind="print"),)),
        ),
    ))

    assert station._run_plan_from_payload({}) is None
    assert station._run_plan_from_payload({"lane": "right"}) is not None
    assert station._run_plan_from_payload({"lane": "missing"}) is None
