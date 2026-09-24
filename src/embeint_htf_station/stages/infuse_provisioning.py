from __future__ import annotations

import asyncio
import tempfile
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from embeint_htf_station.config import ProgrammerSettings, Settings, StageSettings, UicrWriteSettings
from embeint_htf_station.stages.id_pool import IdPoolError, allocate_variables
from embeint_htf_station.stages.base import StageContext, StageLogger, StageOutputValue, StageResult
from embeint_htf_station.stages.nrfutil import (
    NrfutilError,
    _describe_programmer,
    _nrfutil_device_command,
    _resolve_programmer,
    _run_command,
)
from embeint_htf_station.targets import supported_target_devices, target_device_settings


class InfuseProvisioningError(RuntimeError):
    """Raised when Infuse UICR provisioning cannot be prepared."""


@dataclass(frozen=True)
class UicrWrite:
    name: str
    address: int
    value: int
    width_bits: int
    byte_order: str


class InfuseProvisioningStage:
    def __init__(
        self,
        settings: StageSettings,
        programmers: Mapping[str, ProgrammerSettings],
        station_settings: Settings,
        run_command: Callable[[Sequence[str], StageLogger], Awaitable[None]] = _run_command,
    ) -> None:
        self._settings = settings
        self._programmers = programmers
        self._station_settings = station_settings
        self._run_command = run_command

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        await logger.log("info", "stage started")

        try:
            programmer = _resolve_programmer(self._settings, self._programmers)
            dut_id = context.dut_id
            hardware_id_value = context.get_output_value("hardware_id")
            if self._settings.provisioning_source == "infuse_api" and (hardware_id_value is None or not str(hardware_id_value).strip()):
                raise InfuseProvisioningError("infuse_provisioning requires a prior hardware_id stage")
            hardware_id = str(hardware_id_value)
            await logger.log("info", _describe_programmer(programmer))
            if self._settings.board_pool:
                await logger.log("info", f"using board pool {self._settings.board_pool}")

            if self._settings.provisioning_source == "id_pool":
                provisioning_values = await asyncio.to_thread(
                    allocate_variables, self._station_settings, dut_id,
                    _requested_provisioning_keys(self._settings), self._settings.record_version,
                )
            else:
                provisioning_values = self._resolve_infuse_values(programmer, hardware_id)
            for key, value in provisioning_values.items():
                context.set_output(self._settings.name, f"provisioning.{key}", value)

            writes = self._resolve_writes(programmer, dut_id, provisioning_values, context.output_values)
            hex_path = self._write_hex_file(writes, dut_id)
            for write in writes:
                await logger.log(
                    "info",
                    f"prepared UICR {write.name}: address=0x{write.address:08X}, width={write.width_bits}, value=0x{write.value:X}",
                )

            await self._run_command(
                _nrfutil_device_command(("program", "--firmware", str(hex_path)), programmer),
                logger,
            )
            outcome = "passed"
            await logger.log("info", "stage passed")
        except (InfuseProvisioningError, IdPoolError, NrfutilError, OSError) as exc:
            outcome = "failed"
            await logger.log("error", str(exc))

        return StageResult(
            name=self._settings.name,
            outcome=outcome,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    def _resolve_infuse_values(self, programmer: ProgrammerSettings | None, hardware_id: str) -> dict[str, str]:
        url = (
            f"{self._station_settings.api_base_url.rstrip('/')}/api/v1/stations/"
            f"{self._station_settings.station_id}/infuse/provision"
        )
        body = {
            "hardwareId": hardware_id,
            "targetDevice": programmer.target_device if programmer else "",
            "board": programmer.board if programmer and programmer.board else "",
            "boardPool": self._settings.board_pool or "",
            "requestedKeys": list(_requested_provisioning_keys(self._settings)),
        }
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **({"X-Station-Key": self._station_settings.station_key} if self._station_settings.station_key else {}),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:  # nosec B310
                data = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise InfuseProvisioningError(f"failed to resolve Infuse provisioning data: {exc}") from exc

        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            raise InfuseProvisioningError("Infuse provisioning response did not include values")
        return {str(key): str(value) for key, value in values.items()}

    def _resolve_writes(
        self,
        programmer: ProgrammerSettings | None,
        dut_id: str | None,
        provisioning_values: Mapping[str, str],
        context: Mapping[str, StageOutputValue],
    ) -> tuple[UicrWrite, ...]:
        if not self._settings.uicr:
            requested = ", ".join(self._settings.constants) if self._settings.constants else "none"
            raise InfuseProvisioningError(
                "infuse_provisioning requires explicit uicr entries with name, bytes/width_bits, value, and endian "
                f"(requested constants: {requested})",
            )

        uicr_start_address = _uicr_start_address(programmer)
        if uicr_start_address is None and any(write.address is None for write in self._settings.uicr):
            supported = ", ".join(supported_target_devices())
            target_device = programmer.target_device if programmer else None
            raise InfuseProvisioningError(
                f"no UICR start address configured for target_device={target_device!r}; "
                f"supported target devices: {supported}",
            )

        writes: list[UicrWrite] = []
        offset = 0
        for settings in self._settings.uicr:
            default_address = None if uicr_start_address is None else uicr_start_address + offset
            write = _resolve_uicr_write(settings, dut_id, provisioning_values, context, default_address)
            writes.append(write)
            offset += write.width_bits // 8

        names = {write.name for write in writes}
        missing_constants = [constant for constant in self._settings.constants if constant not in names]
        if missing_constants:
            raise InfuseProvisioningError(
                f"constants requested but not mapped to UICR writes: {', '.join(missing_constants)}",
            )
        return tuple(writes)

    def _write_hex_file(self, writes: tuple[UicrWrite, ...], dut_id: str | None) -> Path:
        cache_dir = Path(self._station_settings.firmware_cache_dir).parent / "provisioning"
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_dut_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (dut_id or "direct"))
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            prefix=f"{safe_dut_id}-",
            suffix=".hex",
            dir=cache_dir,
            delete=False,
        )
        with handle:
            handle.write(_intel_hex(writes))
        return Path(handle.name)


def _resolve_uicr_write(
    settings: UicrWriteSettings,
    dut_id: str | None,
    provisioning_values: Mapping[str, str],
    context: Mapping[str, StageOutputValue],
    default_address: int | None,
) -> UicrWrite:
    if not settings.name:
        raise InfuseProvisioningError("UICR write requires name")
    if settings.width_bits not in {8, 16, 32, 64}:
        raise InfuseProvisioningError(
            f"UICR write {settings.name} has unsupported width_bits {settings.width_bits}; "
            "use bytes 1, 2, 4, or 8",
        )
    if settings.byte_order not in {"little", "big"}:
        raise InfuseProvisioningError(
            f"UICR write {settings.name} has unsupported endian {settings.byte_order}; expected LSB or MSB",
        )

    source = settings.source.strip().lower()
    if source in {"auto", ""}:
        value = _resolve_auto_value(settings, dut_id, provisioning_values, context)
    elif source in {"literal", "value"}:
        value = _required_literal_value(settings)
    elif source == "dut_id":
        if dut_id is None or not dut_id.strip():
            raise InfuseProvisioningError(f"UICR write {settings.name} requires a DUT id")
        value = _parse_int(dut_id, "dut_id")
    elif source in {"provisioning", "infuse_api"}:
        key = _strip_reference_prefix(str(settings.value or settings.name), "provisioning")
        resolved = _lookup_mapping(provisioning_values, key)
        if resolved is None:
            raise InfuseProvisioningError(f"Infuse provisioning response did not include {key}")
        value = _parse_int(resolved, f"provisioning.{key}")
    elif source in {"context", "stage"}:
        key = _strip_reference_prefix(str(settings.value or settings.name), "context")
        resolved = _lookup_mapping(context, key)
        if resolved is None:
            raise InfuseProvisioningError(f"stage context did not include {key}")
        value = _parse_int(resolved, f"context.{key}")
    else:
        raise InfuseProvisioningError(
            f"UICR write {settings.name} has unsupported source {settings.source}; "
            "expected auto, literal, dut_id, provisioning, or context",
        )

    max_value = (1 << settings.width_bits) - 1
    if value < 0 or value > max_value:
        raise InfuseProvisioningError(
            f"UICR write {settings.name} value 0x{value:X} does not fit in {settings.width_bits} bits",
        )

    address = settings.address if settings.address is not None else default_address
    if address is None:
        raise InfuseProvisioningError(f"UICR write {settings.name} requires an address or target-device UICR start")

    return UicrWrite(
        name=settings.name,
        address=_parse_int(address, f"uicr.{settings.name}.address"),
        value=value,
        width_bits=settings.width_bits,
        byte_order=settings.byte_order,
    )


def _uicr_start_address(programmer: ProgrammerSettings | None) -> int | None:
    if programmer is None:
        return None
    target = target_device_settings(programmer.target_device)
    return target.uicr_start_address if target is not None else None


def _requested_provisioning_keys(settings: StageSettings) -> tuple[str, ...]:
    keys = set(settings.constants)
    for write in settings.uicr:
        source = write.source.strip().lower()
        if source in {"provisioning", "infuse_api"}:
            keys.add(_strip_reference_prefix(str(write.value or write.name), "provisioning"))
            continue
        if source not in {"auto", ""}:
            continue

        key = str(write.value or write.name).strip()
        if not key or _try_parse_int(key) is not None:
            continue
        key = _strip_reference_prefix(key, "provisioning")
        if key in {"dut_id", "dutId", "hardware_id", "hardwareId", "target_device", "targetDevice"}:
            continue
        if key.startswith("context."):
            continue
        keys.add(key)
    return tuple(sorted(key for key in keys if key))


def _resolve_auto_value(
    settings: UicrWriteSettings,
    dut_id: str | None,
    provisioning_values: Mapping[str, str],
    context: Mapping[str, StageOutputValue],
) -> int:
    if settings.value is None:
        resolved = _lookup_mapping(provisioning_values, settings.name)
        if resolved is None:
            raise InfuseProvisioningError(f"UICR write {settings.name} requires value")
        return _parse_int(resolved, f"provisioning.{settings.name}")

    parsed = _try_parse_int(settings.value)
    if parsed is not None:
        return parsed

    key = str(settings.value).strip()
    if key in {"dut_id", "dutId"}:
        if dut_id is None or not dut_id.strip():
            raise InfuseProvisioningError(f"UICR write {settings.name} requires a DUT id")
        return _parse_int(dut_id, "dut_id")

    context_key = _strip_reference_prefix(key, "context")
    resolved = _lookup_mapping(context, context_key)
    if resolved is not None:
        return _parse_int(resolved, f"context.{context_key}")

    provisioning_key = _strip_reference_prefix(key, "provisioning")
    resolved = _lookup_mapping(provisioning_values, provisioning_key)
    if resolved is not None:
        return _parse_int(resolved, f"provisioning.{provisioning_key}")

    raise InfuseProvisioningError(f"UICR write {settings.name} could not resolve value reference {key}")


def _required_literal_value(settings: UicrWriteSettings) -> int:
    if settings.value is None:
        raise InfuseProvisioningError(f"UICR write {settings.name} requires value for literal source")
    return _parse_int(settings.value, f"uicr.{settings.name}.value")


def _lookup_mapping(values: Mapping[str, StageOutputValue], key: str) -> StageOutputValue | None:
    if key in values:
        value = values[key]
        return value if value != "" else None
    lowered = key.lower()
    for existing_key, value in values.items():
        if existing_key.lower() == lowered:
            return value if value != "" else None
    return None


def _strip_reference_prefix(value: str, prefix: str) -> str:
    value = value.strip()
    marker = f"{prefix}."
    return value[len(marker):] if value.startswith(marker) else value


def _try_parse_int(value: str | int) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        return None


def _parse_int(value: str | int, field_name: str) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError as exc:
        raise InfuseProvisioningError(f"{field_name} must be an integer or 0x-prefixed hex value") from exc


def _intel_hex(writes: tuple[UicrWrite, ...]) -> str:
    lines: list[str] = []
    current_upper: int | None = None
    for write in sorted(writes, key=lambda item: item.address):
        upper = write.address >> 16
        if upper != current_upper:
            lines.append(_record(0x0000, 0x04, upper.to_bytes(2, "big")))
            current_upper = upper

        byte_count = write.width_bits // 8
        data = write.value.to_bytes(byte_count, write.byte_order)
        lines.append(_record(write.address & 0xFFFF, 0x00, data))

    lines.append(":00000001FF")
    return "\n".join(lines) + "\n"


def _record(address: int, record_type: int, data: bytes) -> str:
    payload = bytes((len(data), (address >> 8) & 0xFF, address & 0xFF, record_type, *data))
    checksum = (-sum(payload)) & 0xFF
    return f":{payload.hex().upper()}{checksum:02X}"
