from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from embeint_htf_station.config import StageSettings
from embeint_htf_station.stages.base import Stage, StageContext, StageLogger, StageResult
from embeint_htf_station.stages.values import resolve_value
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
    if "${" not in settings.model_dump_json():
        return factory(settings)
    return _ConfiguredStage(settings, factory)


class _ConfiguredStage:
    def __init__(self, settings: StageSettings, factory: StageFactory) -> None:
        self._settings, self._factory = settings, factory

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        try:
            data = self._settings.model_dump(by_alias=True)
            # Identity, routing, dependency and lock names remain static.
            static = {"name", "kind", "programmer", "after", "locks", "variables"}
            if self._settings.kind == "print":
                static.add("message")  # PrintStage also supports direct use.
            resolved = {key: value if key in static else resolve_value(value, context) for key, value in data.items()}
            settings = StageSettings.model_validate(resolved)
        except ValueError:
            await logger.log("error", "stage configuration has an unavailable value reference or invalid resolved field")
            return StageResult(self._settings.name, "failed", started_at, datetime.now(UTC))
        return await self._factory(settings).run(logger, context)
