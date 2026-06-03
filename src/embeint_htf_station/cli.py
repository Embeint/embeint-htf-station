from __future__ import annotations

import asyncio

import click
import structlog

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.client import connect, heartbeat_loop

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


async def _serve(settings: Settings) -> None:
    async with connect(settings) as client:
        await heartbeat_loop(client, settings)
