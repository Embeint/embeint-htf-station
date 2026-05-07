from __future__ import annotations

from dataclasses import dataclass, field

from embeint_htf_station.config import Settings, StageSettings
from embeint_htf_station.stages import create_stage
from embeint_htf_station.stages.infuse_validation import InfuseValidationStage
from embeint_htf_station.stages.print_stage import PrintStage
from embeint_htf_station.stations.basic import BasicStation


@dataclass
class FakeLogger:
    entries: list[tuple[str, str]] = field(default_factory=list)

    async def log(self, level: str, msg: str) -> None:
        self.entries.append((level, msg))


async def test_print_stage_runs_from_settings(capsys) -> None:
    stage = PrintStage(StageSettings(name="print testing", message="testing", wait_seconds=0))
    logger = FakeLogger()

    result = await stage.run(logger)

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

    result = await stage.run(logger)

    assert result.name == "custom stage"
    assert result.outcome == "failed"
    assert logger.entries == [("error", "unsupported stage kind: missing")]


def test_basic_station_registers_infuse_validation_rtt_alias() -> None:
    station = BasicStation(Settings(org_id="org-1", station_id="station-1"))
    settings = StageSettings(name="Validation", kind="infuse_validation_rtt")

    assert isinstance(create_stage(settings, station._stage_factories), InfuseValidationStage)
