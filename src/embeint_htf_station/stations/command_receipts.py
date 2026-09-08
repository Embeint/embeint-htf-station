from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from embeint_htf_station.config import Settings


class CommandReceiptStore:
    def __init__(self, settings: Settings, max_receipts: int = 2_048) -> None:
        self._directory = (
            Path(settings.firmware_cache_dir).parent
            / "command-receipts"
            / settings.station_id
        )
        self._max_receipts = max_receipts

    def claim(self, command_id: UUID) -> bool:
        self._directory.mkdir(parents=True, exist_ok=True)
        receipt = self._directory / f"{command_id}.receipt"
        try:
            descriptor = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(descriptor, f"{command_id}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._prune(receipt)
        return True

    def _prune(self, protected: Path) -> None:
        receipts = sorted(
            (receipt for receipt in self._directory.glob("*.receipt") if receipt != protected),
            key=lambda receipt: receipt.stat().st_mtime_ns,
            reverse=True,
        )
        for receipt in receipts[max(0, self._max_receipts - 1):]:
            receipt.unlink(missing_ok=True)
