from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from embeint_htf_station.config import Settings

log = structlog.get_logger(__name__)


class ReportOutbox:
    """Capture-bearing stage/result messages survive until the server commits them."""

    def __init__(self, settings: Settings) -> None:
        self.directory = Path(settings.firmware_cache_dir).parent / "report-outbox" / settings.station_id
        self.topic_prefix = settings.topic_prefix

    def store(self, delivery_id: str, topic: str, payload: str) -> None:
        destination = self.directory / f"{UUID(delivery_id)}.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        if not destination.exists() and len(list(self.directory.glob("*.json"))) >= 10_000:
            raise RuntimeError("DUT report outbox is full; synchronize reports before running more tests")
        temporary = self.directory / f".{uuid4()}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps({"topic": topic, "payload": payload}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        self._sync()

    def acknowledge(self, delivery_id: str) -> None:
        try:
            path = self.directory / f"{UUID(delivery_id)}.json"
        except (ValueError, AttributeError):
            return
        if path.exists():
            path.unlink()
            self._sync()

    def pending(self) -> list[tuple[str, str]]:
        messages: list[tuple[str, str]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                content = json.loads(path.read_text())
                if content["topic"] not in {f"{self.topic_prefix}/stage", f"{self.topic_prefix}/result"}:
                    raise ValueError("unexpected report topic")
                messages.append((content["topic"], content["payload"]))
            except FileNotFoundError:
                continue  # An acknowledgement can remove a queued file.
            except (ValueError, KeyError, TypeError):
                log.error("report_outbox.invalid_record", file=path.name)
        return messages

    async def replay(self, client: object) -> None:
        # Publisher is structural; importing the runner here would create a cycle.
        for topic, payload in self.pending():
            await client.publish(topic, payload=payload, qos=1)  # type: ignore[attr-defined]

    async def serve(self, client: object) -> None:
        while True:
            try:
                await self.replay(client)
            except Exception:
                log.warning("report_outbox.sync_pending", pending=len(self.pending()))
            await asyncio.sleep(10)

    def _sync(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
