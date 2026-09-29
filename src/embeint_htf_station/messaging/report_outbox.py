from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

import structlog

from embeint_htf_station.config import Settings

log = structlog.get_logger(__name__)


class ReportOutbox:
    """Reports stay durable until committed, or quarantined with a rejection reason."""

    ACTIVE_LIMIT = 10_000

    def __init__(self, settings: Settings) -> None:
        self.directory = Path(settings.firmware_cache_dir).parent / "report-outbox" / settings.station_id
        self.topic_prefix = settings.topic_prefix

    def _write(self, destination: Path, content: dict) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.parent / f".{uuid4()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps(content).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self._sync(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def store(self, delivery_id: str, topic: str, payload: str) -> None:
        destination = self.directory / f"{UUID(delivery_id)}.json"
        if not destination.exists() and len(list(self.directory.glob("*.json"))) >= self.ACTIVE_LIMIT:
            destination = self.directory / "deferred" / destination.name
            log.warning("report_outbox.backlog_deferred", delivery_id=delivery_id)
        self._write(destination, {"topic": topic, "payload": payload, "attempts": 1, "nextAttemptAt": time.time() + 10})

    def acknowledge(self, delivery_id: str, *, status: str = "accepted", reason: str | None = None) -> None:
        try:
            name = f"{UUID(delivery_id)}.json"
        except (ValueError, AttributeError):
            return
        if status not in {"accepted", "rejected"}:
            log.warning("report_outbox.unknown_ack_status")
            return
        for directory in (self.directory, self.directory / "deferred"):
            path = directory / name
            if not path.exists():
                continue
            if status == "rejected":
                self._quarantine(path, reason if isinstance(reason, str) else "Server rejected this report")
            else:
                path.unlink()
                self._sync(directory)

    def _quarantine(self, path: Path, reason: str) -> None:
        try:
            content = json.loads(path.read_text())
            if not isinstance(content, dict):
                raise ValueError("record is not an object")
        except (ValueError, TypeError):
            content = {"rawRecord": path.read_text()}
        content["rejectionReason"] = reason[:512]
        content["rejectedAt"] = time.time()
        self._write(self.directory / "rejected" / path.name, content)
        path.unlink(missing_ok=True)
        self._sync(path.parent)
        log.error("report_outbox.quarantined", file=path.name, reason=reason[:512])

    def _records(self):
        for path in sorted(self.directory.glob("*.json")):
            try:
                content = json.loads(path.read_text())
                if not isinstance(content, dict) or content.get("topic") not in {
                    f"{self.topic_prefix}/stage", f"{self.topic_prefix}/result",
                } or not isinstance(content.get("payload"), str):
                    raise ValueError("invalid report record")
                envelope = json.loads(content["payload"])
                if str(UUID(envelope["deliveryId"])) != path.stem:
                    raise ValueError("delivery identity does not match record")
                if not isinstance(content.get("attempts", 0), int) or not isinstance(content.get("nextAttemptAt", 0), (int, float)):
                    raise ValueError("invalid retry metadata")
                yield path, content
            except FileNotFoundError:
                continue
            except (ValueError, KeyError, TypeError, AttributeError):
                self._quarantine(path, "Invalid local report record")

    def pending(self) -> list[tuple[str, str]]:
        return [(content["topic"], content["payload"]) for _, content in self._records()]

    def _promote_deferred(self) -> None:
        available = self.ACTIVE_LIMIT - len(list(self.directory.glob("*.json")))
        for path in sorted((self.directory / "deferred").glob("*.json"))[:max(0, available)]:
            os.replace(path, self.directory / path.name)
            self._sync(self.directory)
            self._sync(path.parent)

    async def replay(self, client: object, *, force: bool = True) -> None:
        self._promote_deferred()
        for path, content in self._records():
            if not force and content.get("nextAttemptAt", 0) > time.time():
                continue
            # Save retry state before awaiting MQTT, so an arriving ACK cannot resurrect the file.
            attempts = content.get("attempts", 0) + 1
            content.update(attempts=attempts, nextAttemptAt=time.time() + min(600, 10 * 2 ** min(attempts - 1, 6)))
            self._write(path, content)
            await client.publish(content["topic"], payload=content["payload"], qos=1)  # type: ignore[attr-defined]

    async def serve(self, client: object) -> None:
        while True:
            try:
                await self.replay(client, force=False)
            except Exception:
                log.warning("report_outbox.sync_pending", exc_info=True)
            await asyncio.sleep(10)

    @staticmethod
    def _sync(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
