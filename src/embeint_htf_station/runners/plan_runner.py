from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


async def run_plan(path: Path) -> None:
    log.info("plan.run.start", path=str(path))
    # TODO: parse YAML plan, drive programmer + device, report via BatchLogger.
    log.info("plan.run.done", path=str(path))
