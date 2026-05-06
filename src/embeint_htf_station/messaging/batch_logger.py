from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiomqtt
import structlog

log = structlog.get_logger(__name__)


@dataclass
class _Batch:
    entries: list[dict] = field(default_factory=list)
    byte_size: int = 0


class BatchLogger:
    """Buffers log entries and flushes on 500ms OR 4KB, whichever comes first.

    Why: v1 burned a station by publishing every log entry as its own MQTT message;
    broker and Postgres both buckled under the write amplification.
    """

    FLUSH_INTERVAL_S = 0.5
    FLUSH_BYTES = 4 * 1024

    def __init__(self, client: aiomqtt.Client, topic: str, run_id: str | None = None) -> None:
        self._client = client
        self._topic = topic
        self._run_id = run_id
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
        entry = {"t": datetime.now(UTC).isoformat(), "lvl": level, "msg": msg}
        encoded = json.dumps(entry)
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
        payload = json.dumps({"ts": datetime.now(UTC).isoformat(), "runId": self._run_id, "entries": self._batch.entries})
        await self._client.publish(self._topic, payload=payload, qos=1)
        self._batch = _Batch()
