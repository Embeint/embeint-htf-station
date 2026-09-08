from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from embeint_htf_station.config import Settings
from embeint_htf_station.contracts.mqtt import Command


@dataclass(frozen=True)
class ReceiptRun:
    run_id: str
    dut_id: str
    lane: str


@dataclass(frozen=True)
class CommandReceipt:
    command_id: UUID
    kind: str
    state: str
    runs: tuple[ReceiptRun, ...]


class CommandReceiptStore:
    def __init__(self, settings: Settings, max_receipts: int = 2_048) -> None:
        self._directory = (
            Path(settings.firmware_cache_dir).parent
            / "command-receipts"
            / settings.station_id
        )
        self._max_receipts = max_receipts

    def claim(self, command: Command) -> bool:
        self._directory.mkdir(parents=True, exist_ok=True)
        receipt_path = self._path(command.id)
        receipt = CommandReceipt(command.id, command.kind, "pending", _command_runs(command))
        content = self._serialize(receipt)
        temporary = self._directory / f".{command.id}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, receipt_path)
        except FileExistsError:
            return False
        finally:
            temporary.unlink(missing_ok=True)
        self._sync_directory()
        self._prune(receipt_path)
        return True

    def incomplete(self) -> tuple[CommandReceipt, ...]:
        if not self._directory.exists():
            return ()
        receipts = (self._read(path) for path in self._directory.glob("*.receipt"))
        return tuple(receipt for receipt in receipts if receipt.state == "pending")

    def complete_run(self, command_id: UUID, run_id: str) -> None:
        receipt = self._read(self._path(command_id))
        if receipt.state == "completed":
            return
        remaining = tuple(run for run in receipt.runs if run.run_id != run_id)
        state = "completed" if not remaining else "pending"
        self._replace(replace(receipt, state=state, runs=remaining))

    def complete(self, command_id: UUID) -> None:
        receipt = self._read(self._path(command_id))
        if receipt.state != "completed":
            self._replace(replace(receipt, state="completed", runs=()))

    def _path(self, command_id: UUID) -> Path:
        return self._directory / f"{command_id}.receipt"

    def _read(self, path: Path) -> CommandReceipt:
        try:
            payload = json.loads(path.read_text())
            return CommandReceipt(
                command_id=UUID(str(payload["command_id"])),
                kind=str(payload["kind"]),
                state=str(payload["state"]),
                runs=tuple(ReceiptRun(**run) for run in payload.get("runs", [])),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return CommandReceipt(UUID(path.stem), "legacy", "completed", ())

    @staticmethod
    def _serialize(receipt: CommandReceipt) -> bytes:
        payload = asdict(receipt)
        payload["command_id"] = str(receipt.command_id)
        return (json.dumps(payload, separators=(",", ":")) + "\n").encode()

    def _replace(self, receipt: CommandReceipt) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._path(receipt.command_id)
        temporary = self._directory / f".{receipt.command_id}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, self._serialize(receipt))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        self._sync_directory()

    def _sync_directory(self) -> None:
        descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _prune(self, protected: Path) -> None:
        receipts = sorted(
            self._directory.glob("*.receipt"),
            key=lambda receipt: receipt.stat().st_mtime_ns,
        )
        excess = max(0, len(receipts) - self._max_receipts)
        for receipt_path in receipts:
            if excess == 0:
                break
            if receipt_path == protected or self._read(receipt_path).state != "completed":
                continue
            receipt_path.unlink(missing_ok=True)
            excess -= 1


def _command_runs(command: Command) -> tuple[ReceiptRun, ...]:
    payload = command.payload
    if not isinstance(payload, dict):
        return ()
    if command.kind == "run-plan":
        run = _receipt_run(payload)
        return (run,) if run is not None else ()
    if command.kind != "run-batch" or not isinstance(payload.get("runs"), list):
        return ()
    return tuple(
        run
        for item in payload["runs"]
        if isinstance(item, dict) and (run := _receipt_run(item)) is not None
    )


def _receipt_run(payload: dict[str, object]) -> ReceiptRun | None:
    run_id = payload.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    dut_id = payload.get("dutId")
    lane = payload.get("lane")
    return ReceiptRun(
        run_id=run_id,
        dut_id=dut_id if isinstance(dut_id, str) else "unknown",
        lane=lane if isinstance(lane, str) else "default",
    )
