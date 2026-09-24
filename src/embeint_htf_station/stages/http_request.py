from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from embeint_htf_station.config import HttpRequestSettings, StageSettings
from embeint_htf_station.stages.base import StageContext, StageLogger, StageResult


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send_http(config: HttpRequestSettings, idempotency_key: str | None) -> dict[str, str | int]:
    target = urlsplit(config.url)
    if target.scheme not in {"https", "http"} or not target.hostname or target.username or target.password:
        raise ValueError("HTTP stage requires an HTTP(S) URL without embedded credentials")
    headers = {"Accept": "application/json", **config.headers}
    if idempotency_key and not any(key.lower() == "idempotency-key" for key in headers):
        headers["Idempotency-Key"] = idempotency_key
    body = None if config.json_body is None else json.dumps(config.json_body).encode("utf-8")
    if body is not None and not any(key.lower() == "content-type" for key in headers):
        headers["Content-Type"] = "application/json"
    request = Request(config.url, data=body, headers=headers, method=config.method)
    # No retries or redirects: replaying a request can consume a manufacturing ID twice.
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=config.timeout_seconds)
    except HTTPError as exc:
        response = exc
    with response:
        if response.status not in config.expected_statuses:
            raise ValueError(f"HTTP service returned status {response.status}; reservation retained")
        if not config.outputs:
            return {}
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("HTTP response exceeded 1 MiB; reservation retained")
        payload = json.loads(raw)
    outputs: dict[str, str | int] = {}
    for name, path in config.outputs.items():
        value = payload
        for part in path.split(".") if path else []:
            value = value[int(part)] if isinstance(value, list) else value[part]
        if not name.strip() or isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("HTTP output must be a named string or integer")
        outputs[name] = value
    return outputs


class HttpRequestStage:
    def __init__(self, settings: StageSettings) -> None:
        self._settings = settings

    async def run(self, logger: StageLogger, context: StageContext) -> StageResult:
        started_at = datetime.now(UTC)
        try:
            if self._settings.http is None:
                raise ValueError("http_request requires an http configuration")
            if not context.prerequisites_passed:
                raise ValueError("HTTP request blocked by a failed prerequisite")
            # Stable across run retries, different after an explicit release/re-reserve.
            key = hashlib.sha256(json.dumps([context.dut_id, self._settings.name, sorted(context.reservations.items())]).encode()).hexdigest() if context.reservations else None
            outputs = await asyncio.to_thread(_send_http, self._settings.http, key)
            for name, value in outputs.items():
                context.set_output(self._settings.name, name, value)
            await logger.log("info", "HTTP service stage passed")
            outcome = "passed"
        except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError):
            # Avoid leaking credentials/response content from an exception. Even a
            # timeout may mean the service consumed the ID: never release here.
            await logger.log("error", "HTTP service outcome failed or uncertain; reservation retained. Reconcile with the service before commit or release.")
            outcome = "failed"
        return StageResult(self._settings.name, outcome, started_at, datetime.now(UTC))
