from __future__ import annotations

import asyncio

import click
import structlog

from embeint_htf_station.config import Settings
from embeint_htf_station.messaging.client import connect, heartbeat_loop
from embeint_htf_station.messaging.renewal import CertificateRenewer, CertificateRenewed, renewing_messages
import aiomqtt

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
    renewer = CertificateRenewer(settings)
    while True:
        await asyncio.to_thread(renewer.check)
        try:
            async with connect(settings) as client:
                renewer.connected()
                heartbeat = asyncio.create_task(heartbeat_loop(client, settings))
                try:
                    async for _ in renewing_messages(client, renewer, lambda: True):
                        pass
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
        except CertificateRenewed:
            continue
        except aiomqtt.MqttError:
            await asyncio.sleep(5)
