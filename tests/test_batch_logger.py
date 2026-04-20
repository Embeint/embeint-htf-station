import asyncio
import json
from dataclasses import dataclass, field

import pytest

from embeint_htf_station.messaging.batch_logger import BatchLogger


@dataclass
class _FakeClient:
    published: list[tuple[str, str]] = field(default_factory=list)

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_flush_on_byte_threshold() -> None:
    client = _FakeClient()
    logger = BatchLogger(client, "test/topic")
    big = "x" * (BatchLogger.FLUSH_BYTES + 1)
    await logger.log("info", big)
    assert len(client.published) == 1
    payload = json.loads(client.published[0][1])
    assert len(payload["entries"]) == 1


@pytest.mark.asyncio
async def test_flush_on_interval() -> None:
    client = _FakeClient()
    logger = BatchLogger(client, "test/topic")
    await logger.start()
    await logger.log("info", "small")
    await asyncio.sleep(BatchLogger.FLUSH_INTERVAL_S * 1.5)
    await logger.stop()
    assert len(client.published) >= 1
