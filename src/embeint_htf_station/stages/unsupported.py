from __future__ import annotations

from datetime import UTC, datetime

from embeint_htf_station.config import StageSettings
from embeint_htf_station.stages.base import StageLogger, StageResult


class UnsupportedStage:
    def __init__(self, settings: StageSettings) -> None:
        self._settings = settings

    async def run(self, logger: StageLogger) -> StageResult:
        started_at = datetime.now(UTC)
        finished_at = datetime.now(UTC)
        await logger.log("error", f"unsupported stage kind: {self._settings.kind}")
        return StageResult(
            name=self._settings.name,
            outcome="failed",
            started_at=started_at,
            finished_at=finished_at,
        )
