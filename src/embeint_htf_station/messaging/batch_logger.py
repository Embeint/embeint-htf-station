from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiomqtt
import structlog

from embeint_htf_station.contracts.mqtt import LogBatch, LogBatchEntriesItem

log = structlog.get_logger(__name__)


@dataclass
class _Batch:
    entries: list[LogBatchEntriesItem] = field(default_factory=list)
    byte_size: int = 0


class BatchLogger:
    """Buffers log entries and flushes on 500ms OR 4KB, whichever comes first.

    Why: v1 burned a station by publishing every log entry as its own MQTT message;
    broker and Postgres both buckled under the write amplification.
    """

    FLUSH_INTERVAL_S = 0.5
    FLUSH_BYTES = 4 * 1024

    def __init__(self, client: aiomqtt.Client, topic: str, run_id: str | None = None, lane: str = "default") -> None:
        self._client = client
        self._topic = topic
        self._run_id = run_id
        self._lane = lane
        self._batch = _Batch()
        self._lock = asyncio.Lock()
        self._flusher: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._flusher = asyncio.create_task(self._periodic_flush())

    async def stop(self) -> None:
        if self._flusher:
            self._flusher.cancel()
        await self._flush()

    async def log(self, level: str, msg: str) -> None:
        entry = LogBatchEntriesItem(t=datetime.now(UTC), lvl=level, msg=msg)
        encoded = entry.model_dump_json(by_alias=True)
        async with self._lock:
            self._batch.entries.append(entry)
            self._batch.byte_size += len(encoded)
            if self._batch.byte_size >= self.FLUSH_BYTES:
                await self._flush_locked()

    async def _periodic_flush(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.FLUSH_INTERVAL_S)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._batch.entries:
            return
        payload = LogBatch(
            ts=datetime.now(UTC),
            runId=self._run_id,
            lane=self._lane,
            entries=self._batch.entries,
        ).model_dump_json(by_alias=True)
        await self._client.publish(self._topic, payload=payload, qos=1)
        self._batch = _Batch()
