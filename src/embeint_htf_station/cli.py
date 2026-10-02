from __future__ import annotations

import asyncio
from pathlib import Path

import click
import structlog

from embeint_htf_station.config import (
    ConfigError, Settings, load_settings_from_yaml, parse_runtime_plans_from_yaml_text,
)
from pydantic import ValidationError
from embeint_htf_station.messaging.client import connect, heartbeat_loop
from embeint_htf_station.messaging.renewal import CertificateRenewer, CertificateRenewed, renewing_messages
from embeint_htf_station.messaging.inbox import MessageInbox
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


@main.command("check-config")
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--runtime", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Also validate an exported server runtime YAML against the local programmers.")
def check_config(config: Path, runtime: Path | None) -> None:
    """Validate configuration before an upgrade, without connecting or running hardware."""
    try:
        settings = load_settings_from_yaml(config)
        plans = settings.plans
        if runtime is not None:
            _, plans = parse_runtime_plans_from_yaml_text(runtime.read_text(encoding="utf-8"), settings.programmers)
    except (ConfigError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from None
    except (OSError, ValueError, TypeError):
        raise click.ClickException("Invalid configuration field or unreadable configuration file.") from None
    count = len(plans) or 1
    click.echo(f"Configuration valid: {count} lane plan(s); stage names <=128 UTF-16 units, <=256 stages per plan.")


@main.command()
def run() -> None:
    """Connect to the broker and maintain a heartbeat without running plans."""
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_serve(settings))


async def _serve(settings: Settings) -> None:
    renewer = CertificateRenewer(settings)
    inbox = MessageInbox()
    while True:
        await asyncio.to_thread(renewer.check)
        try:
            async with connect(settings, inbox=inbox) as client:
                renewer.connected()
                heartbeat = asyncio.create_task(heartbeat_loop(client, settings))
                try:
                    async for _ in renewing_messages(client, renewer, lambda: True, inbox=inbox):
                        pass
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
        except CertificateRenewed:
            continue
        except aiomqtt.MqttError:
            await asyncio.sleep(5)
