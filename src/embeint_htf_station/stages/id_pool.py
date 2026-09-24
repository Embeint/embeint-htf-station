from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from embeint_htf_station.config import Settings, StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult


class IdPoolError(RuntimeError):
    """A project variable could not be allocated safely."""


def allocate_variables(
    settings: Settings, dut_id: str, variables: tuple[str, ...], record_version: str | None = None,
) -> dict[str, str]:
    names = tuple(dict.fromkeys(name.strip().lower() for name in variables))
    if not 1 <= len(names) <= 32 or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name == "dut_id" for name in names):
        raise IdPoolError("Request 1–32 valid project variable names")
    if not dut_id.strip():
        raise IdPoolError("A DUT ID is required to allocate variables")
    if not settings.station_key:
        raise IdPoolError("HTF_API_KEY is required to allocate project variables")
    request = Request(
        f"{settings.api_base_url.rstrip('/')}/api/v1/stations/{settings.station_id}/variables/allocate",
        data=json.dumps({"dutId": dut_id, "variables": names, "recordVersion": record_version}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-Station-Key": settings.station_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        descriptions = {403: "station credential rejected", 404: "project variable not found", 409: "ID pool exhausted"}
        raise IdPoolError(descriptions.get(exc.code, f"ID pool request failed (HTTP {exc.code})")) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        # A timeout can follow a committed assignment. Retrying the same DUT is safe.
        raise IdPoolError("ID pool request failed; retry with the same DUT ID") from exc
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict) or data.get("dutId") != dut_id.strip() or any(
        not isinstance(values.get(name), str) or not values[name] for name in names
    ):
        raise IdPoolError("ID pool response is incomplete or belongs to another DUT")
    return {name: values[name] for name in names}


class AllocateVariablesStage:
    def __init__(self, settings: StageSettings, station_settings: Settings) -> None:
        self._settings = settings
        self._station_settings = station_settings

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        try:
            values = await asyncio.to_thread(
                allocate_variables, self._station_settings, context.dut_id,
                self._settings.variables, self._settings.record_version,
            )
            for name, value in values.items():
                context.set_output(self._settings.name, f"provisioning.{name}", value)
            await logger.log("info", f"project variables assigned: {', '.join(values)}")
            outcome = "passed"
        except IdPoolError as exc:
            await logger.log("error", str(exc))
            outcome = "failed"
        return StageResult(self._settings.name, outcome, started_at, datetime.now(UTC))
