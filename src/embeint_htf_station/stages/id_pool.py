from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from embeint_htf_station.config import Settings, StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult


class IdPoolError(RuntimeError):
    """A project variable could not be reserved or committed safely."""


@dataclass(frozen=True)
class PoolReservation:
    values: dict[str, str]
    reservation_ids: dict[str, str]


def _names(variables: tuple[str, ...]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip().lower() for name in variables))
    if not 1 <= len(names) <= 32 or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name == "dut_id" for name in names):
        raise IdPoolError("Request 1–32 valid project variable names")
    return names


def _request(settings: Settings, dut_id: str, names: tuple[str, ...], action: str, body: dict) -> PoolReservation:
    if not dut_id.strip():
        raise IdPoolError("A DUT ID is required to reserve variables")
    if not settings.station_key:
        raise IdPoolError("HTF_API_KEY is required to reserve project variables")
    request = Request(
        f"{settings.api_base_url.rstrip('/')}/api/v1/stations/{settings.station_id}/variables/{action}",
        data=json.dumps({"dutId": dut_id, **body}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-Station-Key": settings.station_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        descriptions = {403: "station credential rejected", 404: "project variable not found", 409: "ID pool exhausted or reservation no longer valid"}
        raise IdPoolError(descriptions.get(exc.code, f"ID pool request failed (HTTP {exc.code})")) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise IdPoolError("ID pool request outcome is uncertain; retry with the same DUT and reservation IDs") from exc
    values = data.get("values") if isinstance(data, dict) else None
    ids = data.get("reservationIds") if isinstance(data, dict) else None
    if not isinstance(values, dict) or not isinstance(ids, dict) or data.get("dutId") != dut_id.strip() or any(
        not isinstance(values.get(name), str) or not values[name] or not isinstance(ids.get(name), str) for name in names
    ):
        raise IdPoolError("ID pool response is incomplete or belongs to another DUT")
    try:
        for name in names:
            if UUID(ids[name]).int == 0:
                raise ValueError("empty token")
    except ValueError as exc:
        raise IdPoolError("ID pool response contains an invalid reservation ID") from exc
    return PoolReservation({name: values[name] for name in names}, {name: ids[name] for name in names})


def allocate_variables(settings: Settings, dut_id: str, variables: tuple[str, ...], record_version: str | None = None) -> PoolReservation:
    """Compatibility function name: allocation now reserves and never commits."""
    names = _names(variables)
    return _request(settings, dut_id, names, "reserve", {"variables": names, "recordVersion": record_version})


def commit_variables(settings: Settings, dut_id: str, reservation_ids: dict[str, str]) -> PoolReservation:
    names = _names(tuple(reservation_ids))
    result = _request(settings, dut_id, names, "commit", {"reservationIds": reservation_ids})
    if result.reservation_ids != reservation_ids:
        raise IdPoolError("Commit response does not match the requested reservations")
    return result


def store_reservation(context: StageContext, stage_name: str, result: PoolReservation) -> None:
    if result.values.keys() != result.reservation_ids.keys():
        raise IdPoolError("Reservation response has mismatched values and IDs")
    # Check the whole response before writing any outputs. A replacement after an
    # external service used the first value must never become this run's commit.
    for name, value in result.values.items():
        if name in context.reservations and (
            context.reservations[name] != result.reservation_ids[name]
            or context.reserved_values[name] != value
        ):
            raise IdPoolError(f"Reservation for {name} changed within this run; reconcile before continuing")
    for name, value in result.values.items():
        context.set_output(stage_name, f"provisioning.{name}", value)
        context.set_reservation(stage_name, name, result.reservation_ids[name], value)


class AllocateVariablesStage:
    def __init__(self, settings: StageSettings, station_settings: Settings) -> None:
        self._settings = settings
        self._station_settings = station_settings

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        try:
            result = await asyncio.to_thread(
                allocate_variables, self._station_settings, context.dut_id,
                self._settings.variables, self._settings.record_version,
            )
            store_reservation(context, self._settings.name, result)
            await logger.log("info", f"project variables reserved: {', '.join(result.values)}")
            outcome = "passed"
        except IdPoolError as exc:
            await logger.log("error", str(exc))
            outcome = "failed"
        return StageResult(self._settings.name, outcome, started_at, datetime.now(UTC))


class CommitVariablesStage:
    def __init__(self, settings: StageSettings, station_settings: Settings) -> None:
        self._settings, self._station_settings = settings, station_settings

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        try:
            if not context.prerequisites_passed:
                raise IdPoolError("Cannot commit after a failed prerequisite")
            names = _names(self._settings.variables)
            if any(name not in context.reservations for name in names):
                raise IdPoolError("Reserve each variable in this run before committing")
            if any(context.get_output_value(f"provisioning.{name}") != context.reserved_values[name] for name in names):
                raise IdPoolError("Run context no longer matches reserved values; reconcile before committing")
            ids = {name: context.reservations[name] for name in names}
            result = await asyncio.to_thread(commit_variables, self._station_settings, context.dut_id, ids)
            if any(context.get_output_value(f"provisioning.{name}") != result.values[name] for name in names):
                raise IdPoolError("Committed values do not match the run context")
            await logger.log("info", f"project variables committed: {', '.join(names)}")
            outcome = "passed"
        except IdPoolError as exc:
            await logger.log("error", str(exc))
            outcome = "failed"
        return StageResult(self._settings.name, outcome, started_at, datetime.now(UTC))
