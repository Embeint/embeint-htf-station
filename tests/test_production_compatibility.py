from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from click.testing import CliRunner

from embeint_htf_station.cli import main
from embeint_htf_station.config import Settings, StageSettings, load_settings_from_yaml, parse_stage_settings_from_yaml_text
from embeint_htf_station.programmers.base import Programmer
from embeint_htf_station.stages import StageContext, StageResult, create_stage
from embeint_htf_station.stages.print_stage import PrintStage
from embeint_htf_station.stations import basic


class Logger:
    def __init__(self):
        self.entries = []

    async def log(self, level, message):
        self.entries.append((level, message))


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, org_id="org", station_id="station",
                    firmware_cache_dir=str(tmp_path / "firmware"), **kwargs)


def test_legacy_plugin_settings_and_programmer_import(tmp_path, monkeypatch):
    monkeypatch.delenv("HTF_PLUGINS", raising=False)
    assert settings(tmp_path, plugins=("custom.stage",)).plugins == ("custom.stage",)
    config = tmp_path / "station.yaml"
    config.write_text("station: {org_id: org, station_id: station}\nplugins: [custom.stage]\n")
    assert load_settings_from_yaml(config).plugins == ("custom.stage",)
    assert Programmer.__annotations__ == {"name": "str"}
    assert all(hasattr(Programmer, method) for method in ("flash", "erase", "reset"))


@pytest.mark.parametrize("factory", [PrintStage, create_stage])
async def test_legacy_print_placeholders_remain_literal(factory):
    logger = Logger()
    stage = factory(StageSettings(name="Print", message="${unknown} ${dut_id}", wait_seconds=0))
    result = await stage.run(logger, StageContext("DUT"))
    assert result.outcome == "passed"
    assert ("info", "${unknown} ${dut_id}") in logger.entries


async def test_custom_stage_literal_configuration_and_explicit_interpolation():
    received = []

    class Custom:
        def __init__(self, stage):
            received.append(stage.path)

        async def run(self, logger, context):
            now = datetime.now(UTC)
            return StageResult("Custom", "passed", now, now)

    for interpolate in (False, True):
        stage = create_stage(StageSettings(name="Custom", kind="custom", path="${dut_id}", interpolate=interpolate),
                             {"custom": Custom})
        await stage.run(Logger(), StageContext("DUT"))
    assert received == ["${dut_id}", "DUT"]
    stage = create_stage(StageSettings(name="Custom", kind="custom", path="${missing}", interpolate=True),
                         {"custom": Custom})
    assert (await stage.run(Logger(), StageContext("DUT"))).outcome == "failed"
    assert len(received) == 2


def test_yaml_secret_dependencies_and_interpolation_are_explicit():
    parsed = parse_stage_settings_from_yaml_text("""
stages:
  - name: Custom
    interpolate: true
    required_secrets: [API_TOKEN]
    message: '${dut_id}'
""")
    assert parsed[0].interpolate is True
    assert parsed[0].required_secrets == ("API_TOKEN",)
    assert StageSettings(name="Legacy").interpolate is False
    assert StageSettings(name="Legacy").required_secrets == ()


@pytest.mark.parametrize("error", [TimeoutError(), URLError("offline"),
                                    HTTPError("https://api.example", 503, "offline", None, None),
                                    ValueError("malformed response"), KeyError("secrets"),
                                    IncompleteRead(b"partial"), RemoteDisconnected("offline")])
async def test_secret_outage_keeps_mqtt_and_background_retry_recovers(tmp_path, monkeypatch, error):
    station = basic.BasicStation(settings(tmp_path, station_key="key", station_secrets={"TOKEN": "stale"}))
    fresh_loaded = asyncio.Event()
    connected = []
    calls = []

    def fetch():
        calls.append(True)
        if len(calls) == 1:
            raise error
        return {"TOKEN": "fresh"}

    original_load = station._load_station_secrets
    original_retry = station._retry_station_secrets

    async def observe_load():
        loaded = await original_load()
        if loaded:
            fresh_loaded.set()
        return loaded

    async def retry():
        # Keep the actual loader and retry loop, avoiding a five-second test wait.
        await original_retry(retry_seconds=0.001)

    async def messages(*args, **kwargs):
        await fresh_loaded.wait()
        if False:
            yield None

    @asynccontextmanager
    async def connect(*args, **kwargs):
        connected.append(dict(station._settings.station_secrets))
        yield SimpleNamespace(subscribe=AsyncMock(), publish=AsyncMock())

    monkeypatch.setattr(station._certificate_renewer, "check", lambda: None)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())
    monkeypatch.setattr(station, "_fetch_station_secrets", fetch)
    monkeypatch.setattr(station, "_load_station_secrets", observe_load)
    monkeypatch.setattr(station, "_retry_station_secrets", retry)
    monkeypatch.setattr(basic, "connect", connect)
    monkeypatch.setattr(basic, "renewing_messages", messages)
    await asyncio.wait_for(station.serve_forever(), timeout=2)
    assert connected == [{}]
    assert len(calls) == 2
    assert station._settings.station_secrets == {"TOKEN": "fresh"}


async def test_secret_failure_blocks_whole_plan_before_hardware_then_fresh_run_succeeds(tmp_path, monkeypatch):
    executed = []

    class Hardware:
        def __init__(self, stage):
            self.stage = stage
            executed.append(("created", stage.name))

        async def run(self, logger, context):
            executed.append(("run", self.stage.name))
            if self.stage.required_secrets:
                assert context.require_secret("TOKEN") == "fresh"
            now = datetime.now(UTC)
            return StageResult(self.stage.name, "passed", now, now)

    station = basic.BasicStation(settings(tmp_path, station_key="key", station_secrets={"TOKEN": "stale"}),
                                 stage_factories={"hardware": Hardware})
    stages = (StageSettings(name="Hardware", kind="hardware"),
              StageSettings(name="Secret service", kind="hardware", required_secrets=("TOKEN",)))

    def unavailable():
        raise TimeoutError()

    monkeypatch.setattr(station, "_fetch_station_secrets", unavailable)
    assert await station._load_station_secrets() is False
    client = SimpleNamespace(publish=AsyncMock())
    failed = await station._run_test(client, "DUT", str(uuid4()), stages=stages)
    assert failed.outcome == "error"
    assert executed == []
    monkeypatch.setattr(station, "_fetch_station_secrets", lambda: {"TOKEN": "fresh"})
    assert await station._load_station_secrets() is True
    passed = await station._run_test(client, "DUT", str(uuid4()), stages=stages)
    assert passed.outcome == "passed"
    assert executed == [("created", "Hardware"), ("run", "Hardware"),
                        ("created", "Secret service"), ("run", "Secret service")]


async def test_legacy_run_once_executes_without_secret_api(tmp_path, monkeypatch):
    station = basic.BasicStation(settings(tmp_path, station_key="key",
        stages=(StageSettings(name="Print", message="literal ${unknown}", wait_seconds=0),)))
    monkeypatch.setattr(station._certificate_renewer, "check", lambda: None)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())

    def unavailable():
        raise TimeoutError()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield SimpleNamespace(publish=AsyncMock())

    monkeypatch.setattr(station, "_fetch_station_secrets", unavailable)
    monkeypatch.setattr(basic, "connect", connect)
    assert (await station.run_once("DUT")).outcome == "passed"


@pytest.mark.parametrize("runtime", ["stages:\n  - name: " + "x" * 129,
                                      "stages:\n" + "".join(f"  - name: stage-{i}\n" for i in range(257))])
def test_offline_upgrade_preflight_rejects_unsafe_runtime_plans(tmp_path, monkeypatch, runtime):
    monkeypatch.setattr(basic, "connect", lambda *a, **kw: pytest.fail("Preflight must not connect"))
    config = tmp_path / "station.yaml"
    config.write_text("station: {org_id: org, station_id: station}\n")
    exported = tmp_path / "runtime.yaml"
    exported.write_text(runtime)
    result = CliRunner().invoke(main, ["check-config", str(config), "--runtime", str(exported)])
    assert result.exit_code == 1
    assert "128 characters" in result.output or "256 stages" in result.output


def test_offline_upgrade_preflight_accepts_boundary_plan(tmp_path):
    config = tmp_path / "station.yaml"
    config.write_text("station: {org_id: org, station_id: station}\nstages:\n" +
                      "".join(f"  - name: {'x' * 120}{i:08}\n" for i in range(256)))
    result = CliRunner().invoke(main, ["check-config", str(config)])
    assert result.exit_code == 0
    assert "Configuration valid" in result.output


async def test_retried_http_error_response_is_closed(tmp_path, monkeypatch):
    from io import BytesIO

    response = BytesIO(b"upstream unavailable")
    station = basic.BasicStation(settings(tmp_path, station_key="key"))

    def unavailable():
        raise HTTPError("https://api.example", 503, "offline", None, response)

    monkeypatch.setattr(basic, "urlopen", lambda *args, **kwargs: unavailable())
    assert await station._load_station_secrets() is False
    assert response.closed


@pytest.mark.parametrize("body", [b"[]", b"{}", b'{"secrets": null}',
                                   b'{"secrets": {"TOKEN": "fresh", "BAD": 42}}', b"not-json"])
async def test_invalid_secret_response_never_exposes_partial_or_stale_values(tmp_path, monkeypatch, body):
    from io import BytesIO

    station = basic.BasicStation(settings(tmp_path, station_key="key", station_secrets={"TOKEN": "stale"}))
    monkeypatch.setattr(basic, "urlopen", lambda *args, **kwargs: BytesIO(body))
    assert await station._load_station_secrets() is False
    assert station._settings.station_secrets == {}


@pytest.mark.parametrize("yaml_text", ["mqtt: {port: invalid}\n", "stages: [not-valid\n",
    "stages:\n  - name: " + "x" * 129,
    "stages:\n" + "".join(f"  - name: stage-{i}\n" for i in range(257))])
def test_offline_upgrade_preflight_rejects_invalid_local_config(tmp_path, yaml_text):
    config = tmp_path / "station.yaml"
    config.write_text("station: {org_id: org, station_id: station}\n" + yaml_text)
    result = CliRunner().invoke(main, ["check-config", str(config)])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output
