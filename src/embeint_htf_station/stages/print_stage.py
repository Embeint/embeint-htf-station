from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from embeint_htf_station.config import StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult


class PrintStage:
    def __init__(self, settings: StageSettings) -> None:
        self._settings = settings

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        await logger.log("info", self._settings.message)
        await asyncio.sleep(self._settings.wait_seconds)

        finished_at = datetime.now(UTC)
        await logger.log("info", "stage passed")
        return StageResult(
            name=self._settings.name,
            outcome="passed",
            started_at=started_at,
            finished_at=finished_at,
        )
