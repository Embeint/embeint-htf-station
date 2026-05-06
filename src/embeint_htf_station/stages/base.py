from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StageResult:
    name: str
    outcome: str
    started_at: datetime
    finished_at: datetime


class StageLogger(Protocol):
    async def log(self, level: str, msg: str) -> None: ...


class Stage(Protocol):
    async def run(self, logger: StageLogger) -> StageResult: ...
