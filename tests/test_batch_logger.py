import asyncio
import json
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from embeint_htf_station.messaging.batch_logger import BatchLogger
from embeint_htf_station.stations.basic import StageScopedLogger


@dataclass
class _FakeClient:
    published: list[tuple[str, str]] = field(default_factory=list)

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_flush_on_byte_threshold() -> None:
    client = _FakeClient()
    run_id = str(uuid4())
    logger = BatchLogger(client, "test/topic", run_id=run_id, lane="left")
    big = "x" * (BatchLogger.FLUSH_BYTES + 1)
    await logger.log("info", big)
    assert len(client.published) == 1
    payload = json.loads(client.published[0][1])
    assert len(payload["entries"]) == 1
    assert payload["runId"] == run_id
    assert payload["lane"] == "left"


@pytest.mark.asyncio
async def test_flush_on_interval() -> None:
    client = _FakeClient()
    logger = BatchLogger(client, "test/topic")
    await logger.start()
    await logger.log("info", "small")
    await asyncio.sleep(BatchLogger.FLUSH_INTERVAL_S * 1.5)
    await logger.stop()
    assert len(client.published) >= 1


@pytest.mark.asyncio
async def test_stage_scoped_logger_formats_stage_output(capsys: pytest.CaptureFixture[str]) -> None:
    client = _FakeClient()
    logger = BatchLogger(client, "test/topic")
    stage_logger = StageScopedLogger(logger, "print testing")

    await stage_logger.start()
    await stage_logger.log("info", "testing")
    await logger.stop()

    assert len(client.published) == 1
    payload = json.loads(client.published[0][1])
    assert payload["entries"][0]["msg"] == "=======print testing======="
    assert payload["entries"][1]["msg"].endswith("[print testing] - testing")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "=======print testing======="
    assert out[1].endswith("[print testing] - testing")


@pytest.mark.asyncio
async def test_stage_scoped_logger_strips_ansi_escape_sequences(capsys: pytest.CaptureFixture[str]) -> None:
    client = _FakeClient()
    logger = BatchLogger(client, "test/topic")
    stage_logger = StageScopedLogger(logger, "Validation")

    await stage_logger.log("info", "\x1b[1;33m<wrn> modem warning\x1b[0m")
    await logger.stop()

    payload = json.loads(client.published[0][1])
    assert payload["entries"][0]["msg"].endswith("[Validation] - <wrn> modem warning")
    assert "\x1b" not in payload["entries"][0]["msg"]
    out = capsys.readouterr().out
    assert "\x1b" not in out
