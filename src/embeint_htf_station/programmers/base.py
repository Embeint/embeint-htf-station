from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Programmer(Protocol):
    """Compatibility protocol for custom station programmer implementations."""

    name: str

    async def flash(self, image: Path) -> None: ...
    async def erase(self) -> None: ...
    async def reset(self) -> None: ...
