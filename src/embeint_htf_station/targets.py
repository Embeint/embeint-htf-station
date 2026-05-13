from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HardwareIdTarget:
    address: int
    words: int


@dataclass(frozen=True)
class TargetDeviceSettings:
    hardware_id: HardwareIdTarget | None = None
    uicr_start_address: int | None = None


_TARGETS: dict[str, TargetDeviceSettings] = {
    "nrf54l15_m33": TargetDeviceSettings(
        hardware_id=HardwareIdTarget(address=0x00FFC304, words=2),
        uicr_start_address=0x00FFD500,
    ),
    "nrf54l15_xxca": TargetDeviceSettings(
        hardware_id=HardwareIdTarget(address=0x00FFC304, words=2),
        uicr_start_address=0x00FFD500,
    ),
}


def target_device_settings(target_device: str | None) -> TargetDeviceSettings | None:
    return _TARGETS.get((target_device or "").strip().lower())


def supported_target_devices() -> tuple[str, ...]:
    return tuple(sorted(_TARGETS))
