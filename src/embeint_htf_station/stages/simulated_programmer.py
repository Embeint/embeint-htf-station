from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

from embeint_htf_station.config import ProgrammerSettings, StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult


class SimulatedProgrammerStage:
    """Exercise the normal lane runtime without connecting to a programmer."""

    def __init__(self, settings: StageSettings, programmers: Mapping[str, ProgrammerSettings]) -> None:
        self._settings = settings
        self._programmers = programmers

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        programmer = self._programmers.get(self._settings.programmer or "")
        if programmer is None:
            await logger.log("error", "simulated_programmer stage requires a configured programmer")
            return StageResult(self._settings.name, "failed", started_at, datetime.now(UTC))
        if programmer.kind != "simulated":
            await logger.log("error", f"simulated_programmer requires kind=simulated, got {programmer.kind}")
            return StageResult(self._settings.name, "failed", started_at, datetime.now(UTC))

        await logger.log("info", "stage started")
        await logger.log("info", f"simulated programmer {programmer.name}: {self._settings.message}")
        await asyncio.sleep(self._settings.wait_seconds)
        context.set_output(self._settings.name, f"{programmer.name}_simulated", "passed")
        await logger.log("info", "stage passed")
        return StageResult(self._settings.name, "passed", started_at, datetime.now(UTC))
