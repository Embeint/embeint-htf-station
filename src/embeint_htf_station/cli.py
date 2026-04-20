from __future__ import annotations

import asyncio
from pathlib import Path

import click
import structlog

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.client import connect, heartbeat_loop
from embeint_htf_station.runners.plan_runner import run_plan

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
log = structlog.get_logger("htf-station")


@click.group()
def main() -> None:
    """Embeint HTF station runtime."""


@main.command()
def run() -> None:
    """Connect to the broker and start the heartbeat loop."""
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_serve(settings))


@main.command()
@click.argument("plan", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def plan(plan: Path) -> None:
    """Execute a test plan YAML file."""
    asyncio.run(run_plan(plan))


async def _serve(settings: Settings) -> None:
    async with connect(settings) as client:
        await heartbeat_loop(client, settings)
