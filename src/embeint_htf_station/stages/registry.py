from __future__ import annotations

from collections.abc import Callable, Mapping

from embeint_htf_station.config import StageSettings
from embeint_htf_station.stages.base import Stage
from embeint_htf_station.stages.print_stage import PrintStage
from embeint_htf_station.stages.unsupported import UnsupportedStage

StageFactory = Callable[[StageSettings], Stage]


def default_stage_factories() -> dict[str, StageFactory]:
    return {
        "print": PrintStage,
    }


def create_stage(settings: StageSettings, factories: Mapping[str, StageFactory] | None = None) -> Stage:
    stage_factories = factories or default_stage_factories()
    factory = stage_factories.get(settings.kind)
    if factory is None:
        return UnsupportedStage(settings)
    return factory(settings)
