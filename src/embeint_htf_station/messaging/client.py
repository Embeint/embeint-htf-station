from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import aiomqtt
import structlog

from embeint_htf_station.config import Settings

log = structlog.get_logger(__name__)


@asynccontextmanager
async def connect(settings: Settings) -> AsyncIterator[aiomqtt.Client]:
    async with aiomqtt.Client(
        hostname=settings.broker_host,
        port=settings.broker_port,
        username=settings.broker_username,
        password=settings.broker_password,
        identifier=f"station-{settings.station_id}",
    ) as client:
        log.info("broker.connected", host=settings.broker_host, port=settings.broker_port)
        yield client


async def heartbeat_loop(client: aiomqtt.Client, settings: Settings, interval_s: float = 5.0) -> None:
    topic = f"{settings.topic_prefix}/heartbeat"
    while True:
        payload = '{"ts":"%s","status":"idle"}' % _utc_now_iso()
        await client.publish(topic, payload=payload, qos=1)
        await asyncio.sleep(interval_s)


def _utc_now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()
