from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from embeint_htf_station.config import ConfigError, Settings, StageSettings, parse_stage_settings_from_yaml_text
from embeint_htf_station.messaging.report_outbox import ReportOutbox
from embeint_htf_station.stages import StageContext, StageResult
from embeint_htf_station.stages.infuse_validation import InfuseValidationStage
from embeint_htf_station.stations.basic import BasicStation
from tests.test_infuse_validation import FakeTransport, Logger


async def test_run_once_keeps_captures_local_and_off_the_wire(tmp_path, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from embeint_htf_station.stations import basic

    class CaptureStage:
        def __init__(self, settings):
            self.settings = settings

        async def run(self, logger, context):
            context.capture("sim.iccid", "0000123")
            now = datetime.now(UTC)
            return StageResult(self.settings.name, "passed", now, now)

    publisher = Publisher()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield publisher

    station = BasicStation(Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware"), stages=(StageSettings(name="Read", kind="probe"),)),
        stage_factories={"probe": CaptureStage})
    monkeypatch.setattr(basic, "connect", connect)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())
    monkeypatch.setattr(station, "_load_station_secrets", AsyncMock())
    result = await station.run_once("DUT")
    assert result.report()["observations"][0]["value"] == "0000123"
    for topic, message in publisher.messages:
        if topic.endswith(("/stage", "/result")):
            assert not message.get("observations")
            assert not message.get("deliveryId")
    assert not station._report_outbox.pending()


async def test_rejected_report_is_quarantined_and_not_replayed(tmp_path) -> None:
    settings = Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware"))
    outbox = ReportOutbox(settings)
    delivery = str(uuid4())
    outbox.store(delivery, f"{settings.topic_prefix}/result", json.dumps({"deliveryId": delivery}))
    outbox.acknowledge(delivery, status="rejected", reason="Capture source does not match its stage update.")
    restarted = ReportOutbox(settings)
    replay = Publisher()
    await restarted.replay(replay)
    assert not replay.messages
    quarantined = list((outbox.directory / "rejected").glob("*.json"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text())["rejectionReason"] == "Capture source does not match its stage update."


async def test_retry_backoff_survives_restart_and_active_limit_defers_without_loss(tmp_path, monkeypatch) -> None:
    from embeint_htf_station.messaging import report_outbox
    clock = [1000.0]
    monkeypatch.setattr(report_outbox.time, "time", lambda: clock[0])
    monkeypatch.setattr(ReportOutbox, "ACTIVE_LIMIT", 1)
    settings = Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware"))
    outbox = ReportOutbox(settings)
    ids = [str(uuid4()), str(uuid4())]
    for delivery in ids:
        outbox.store(delivery, f"{settings.topic_prefix}/result", json.dumps({"deliveryId": delivery}))
    assert len(list((outbox.directory / "deferred").glob("*.json"))) == 1
    publisher = Publisher()
    await outbox.replay(publisher, force=False)
    assert not publisher.messages
    clock[0] = 1010
    await outbox.replay(publisher, force=False)
    assert len(publisher.messages) == 1
    restarted = ReportOutbox(settings)
    clock[0] = 1020
    await restarted.replay(publisher, force=False)
    assert len(publisher.messages) == 1
    clock[0] = 1030
    await restarted.replay(publisher, force=False)
    assert len(publisher.messages) == 2
    restarted.acknowledge(ids[0])
    await restarted.replay(publisher)
    assert publisher.messages[-1][1]["deliveryId"] == ids[1]
    restarted.acknowledge(ids[1])
    assert not restarted.pending()


def test_bad_local_outbox_record_is_retained_outside_retry_queue(tmp_path) -> None:
    settings = Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware"))
    outbox = ReportOutbox(settings)
    outbox.directory.mkdir(parents=True)
    (outbox.directory / f"{uuid4()}.json").write_text("bad json")
    assert not outbox.pending()
    assert len(list((outbox.directory / "rejected").glob("*.json"))) == 1


async def test_run_plan_uses_the_only_named_lane(tmp_path, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from embeint_htf_station.config import LanePlanSettings, LaneSettings
    from embeint_htf_station.contracts.mqtt import Command
    from embeint_htf_station.stations import basic

    finished = asyncio.Event()
    run_id = str(uuid4())
    command = Command(id=uuid4(), kind="run-plan", payload={"runId": run_id, "dutId": "DUT"})

    class NamedPublisher(Publisher):
        async def publish(self, topic, payload, qos=0):
            await super().publish(topic, payload, qos)
            if topic.endswith("/result"):
                finished.set()

    publisher = NamedPublisher()

    async def messages():
        yield SimpleNamespace(payload=command.model_dump_json().encode())
        await finished.wait()

    publisher.messages_stream = messages()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield SimpleNamespace(messages=publisher.messages_stream, publish=publisher.publish, subscribe=AsyncMock())

    station = BasicStation(Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware"), lanes=(LaneSettings(name="modem", programmer="probe"),),
        plans=(LanePlanSettings(lane="modem", stages=(StageSettings(name="Read", kind="print", wait_seconds=0),)),)))
    monkeypatch.setattr(basic, "connect", connect)
    monkeypatch.setattr(station, "_load_runtime_configuration", AsyncMock())
    monkeypatch.setattr(station, "_load_station_secrets", AsyncMock())
    await asyncio.wait_for(station._serve_session(), 5)
    final = next(message for topic, message in publisher.messages if topic.endswith("/result"))
    assert final["lane"] == "modem"
    assert final["runId"] == run_id
    assert final["outcome"] == "passed"


def test_capture_keeps_changed_verification_and_subtest_for_the_same_value() -> None:
    context = StageContext("DUT")
    context.begin_stage(0, "Read")
    context.capture("sim.iccid", "0000123", verified=False, subtest="before")
    context.capture("sim.iccid", "0000123", verified=True, subtest="MODEM")
    observations, issues = context.finish_captures("failed", {})
    assert not issues
    assert len(observations) == 2
    assert observations[1].verified and observations[1].subtest == "MODEM"


@pytest.mark.parametrize("source", ["on", "yes", "null", "1.5", "0x12", "12:34", ".nan", "2026-09-29", "!!bool true"])
def test_capture_mapping_rejects_yaml_non_strings(source) -> None:
    with pytest.raises(ConfigError):
        parse_stage_settings_from_yaml_text(f"stages:\n  - name: Read\n    capture: {{ sim.iccid: {source} }}\n")


@pytest.mark.parametrize("source", ["'on'", '"yes"', "!!str null", "'1.5'", "validation.modem.iccid"])
def test_capture_mapping_accepts_explicit_strings(source) -> None:
    assert parse_stage_settings_from_yaml_text(f"stages:\n  - name: Read\n    capture: {{ sim.iccid: {source} }}\n")[0].capture


def test_empty_capture_mapping_is_optional() -> None:
    assert parse_stage_settings_from_yaml_text("stages:\n  - name: Read\n    capture:\n")[0].capture == {}


async def test_direct_capture_and_storage_errors_do_not_change_hardware_outcome(tmp_path, monkeypatch) -> None:
    class OfflinePublisher(Publisher):
        async def publish(self, topic, payload, qos=0):
            if json.loads(payload).get("deliveryId"):
                raise OSError("offline")
            await super().publish(topic, payload, qos)

    class CaptureStage:
        def __init__(self, settings):
            self.settings = settings

        async def run(self, logger, context):
            assert not context.capture("sim.iccid", "")
            assert not context.capture("custom.long", "x" * 513)
            context.capture("modem.imei", "0000123")
            now = datetime.now(UTC)
            return StageResult(self.settings.name, "passed", now, now)

    station = BasicStation(Settings(_env_file=None, org_id=str(uuid4()), station_id=str(uuid4()),
        firmware_cache_dir=str(tmp_path / "firmware")), stage_factories={"probe": CaptureStage})

    def unavailable(*args):
        raise OSError("disk full")

    monkeypatch.setattr(station._report_outbox, "store", unavailable)
    result = await station._run_test(OfflinePublisher(), "DUT", str(uuid4()), stages=(StageSettings(name="Read", kind="probe"),))
    assert result.outcome == "passed"
    assert result.captured_values == {"modem.imei": "0000123"}
    assert len(result.report()["captureIssues"]) == 2


class Publisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.messages.append((topic, json.loads(payload)))


def test_stage_scoped_mapping_does_not_reuse_an_old_output_or_export_secrets() -> None:
    context = StageContext("DUT", secrets={"secret": "private"})
    context.begin_stage(0, "read")
    context.set_output("read", "hardware_id", "00001234")
    captures, issues = context.finish_captures("passed", {"modem.hardware_id": "hardware_id"})
    assert captures[0].value == "00001234"
    assert captures[0].verified
    assert not issues
    context.begin_stage(1, "retest")
    captures, issues = context.finish_captures("failed", {"modem.hardware_id": "hardware_id"})
    assert not captures
    assert "not produced by this stage" in issues[0]
    assert context.get_output_value("hardware_id") == "00001234"
    assert "private" not in str(issues)


def test_conflicting_captures_are_retained_unverified() -> None:
    context = StageContext("DUT")
    context.begin_stage(0, "read")
    context.capture("sim.iccid", "000001")
    context.capture("sim.iccid", "000001")
    context.capture("sim.iccid", "000002")
    observations, issues = context.finish_captures("passed", {})
    assert [item.value for item in observations] == ["000001", "000002"]
    assert [item.sequence for item in observations] == [0, 1]
    assert all(not item.verified for item in observations)
    assert len(issues) == 1


def test_run_capture_limit_matches_the_server_report_limit() -> None:
    context = StageContext("DUT")
    for index in range(16):
        context.begin_stage(index, "Read")
        for field in range(64):
            context.capture(f"custom.value{field}", "0001")
        captures, issues = context.finish_captures("passed", {})
        assert len(captures) == 64
        assert not issues
    context.begin_stage(16, "Read")
    assert not context.capture("custom.extra", "0001")
    captures, issues = context.finish_captures("passed", {})
    assert not captures
    assert "1024 per run" in issues[0]


@pytest.mark.parametrize("mapping", ["[]", "{ Bad: hardware_id }", "{ modem.imei: 123 }", "{ modem.imei: '' }"])
def test_invalid_capture_configuration_identifies_the_stage(mapping: str) -> None:
    with pytest.raises(ConfigError, match="stage.*capture"):
        parse_stage_settings_from_yaml_text(f"stages:\n  - name: Read ID\n    capture: {mapping}\n")


async def test_modem_pass_exports_identifiers_when_another_subtest_fails() -> None:
    settings = StageSettings(name="Validate", tests=("MODEM", "IMU"), capture={
        "modem.imei": "validation.modem.imei", "sim.iccid": "validation.modem.iccid",
        "sim.imsi": "validation.modem.imsi",
    })
    stage = InfuseValidationStage(settings, {}, transport=FakeTransport([
        "000001:MODEM:INFO:Modem IMEI: 001234567890123",
        "000002:MODEM:INFO:ICCID: 00001234567890123456",
        "000003:MODEM:INFO:IMSI: 001234567890123",
        "000004:MODEM:PASS:PASSED",
        "000005:IMU:FAIL:FAILED",
        "000006:SYS:ERROR:Complete with 1/2 passed",
    ]))
    context = StageContext("DUT")
    context.begin_stage(0, settings.name)
    result = await stage.run(Logger(), context)
    assert result.outcome == "failed"
    captures, issues = context.finish_captures(result.outcome, settings.capture)
    assert not issues
    assert {item.key: item.value for item in captures} == {
        "modem.imei": "001234567890123", "sim.iccid": "00001234567890123456", "sim.imsi": "001234567890123",
    }
    assert all(item.verified and item.subtest == "MODEM" for item in captures)


async def test_parallel_lane_reports_remain_independent_and_replay_after_restart(tmp_path) -> None:
    started: set[str] = set()
    both_started = asyncio.Event()

    class CaptureStage:
        def __init__(self, settings: StageSettings) -> None:
            self.settings = settings

        async def run(self, logger: object, context: StageContext) -> StageResult:
            started.add(self.settings.name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            context.capture(f"{self.settings.name}.hardware_id", "0000" + self.settings.name)
            now = datetime.now(UTC)
            return StageResult(self.settings.name, "passed", now, now)

    settings = Settings(org_id=str(uuid4()), station_id=str(uuid4()), firmware_cache_dir=str(tmp_path / "firmware"))
    station = BasicStation(settings, stage_factories={"probe": CaptureStage})
    publisher = Publisher()
    results = await asyncio.gather(*[
        station._run_test(publisher, "DUT", str(uuid4()), stages=(StageSettings(name=lane, kind="probe"),), lane=lane)
        for lane in ("bluetooth", "modem")
    ])
    assert results[0].captured_values == {"bluetooth.hardware_id": "0000bluetooth"}
    assert results[1].captured_values == {"modem.hardware_id": "0000modem"}
    report = results[1].report()
    assert report["dutId"] == "DUT"
    assert report["lane"] == "modem"
    assert report["observations"][0]["key"] == "modem.hardware_id"
    json.dumps(report)
    stages = [message for topic, message in publisher.messages if topic.endswith("/stage") and message.get("observations")]
    finals = [message for topic, message in publisher.messages if topic.endswith("/result")]
    assert len(stages) == len(finals) == 2
    for final in finals:
        source = next(stage for stage in stages if stage["runId"] == final["runId"])
        assert source["observations"] == final["observations"]
        assert source["dutId"] == "DUT"
    restarted = ReportOutbox(settings)
    assert len(restarted.pending()) == 4
    replay = Publisher()
    await restarted.replay(replay)
    for _, message in replay.messages:
        restarted.acknowledge(message["deliveryId"])
    assert not restarted.pending()


async def test_cancellation_retains_active_stage_captures(tmp_path) -> None:
    started = asyncio.Event()

    class InterruptedStage:
        def __init__(self, settings: StageSettings) -> None:
            self.settings = settings

        async def run(self, logger: object, context: StageContext) -> StageResult:
            context.capture("modem.imei", "000123456789012")
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    station = BasicStation(Settings(
        org_id=str(uuid4()), station_id=str(uuid4()), firmware_cache_dir=str(tmp_path / "firmware"),
    ), stage_factories={"probe": InterruptedStage})
    publisher = Publisher()
    task = asyncio.create_task(station._run_test(
        publisher, "DUT", str(uuid4()), stages=(StageSettings(name="Read", kind="probe"),),
    ))
    await started.wait()
    task.cancel()
    result = await task
    assert result.outcome == "aborted"
    assert result.observations[0].value == "000123456789012"
    assert not result.observations[0].verified
    assert not result.captured_values


async def test_transport_failure_keeps_capture_report_without_changing_test_outcome(tmp_path) -> None:
    class OfflinePublisher(Publisher):
        async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
            if json.loads(payload).get("deliveryId"):
                raise OSError("offline")
            await super().publish(topic, payload, qos)

    class CaptureStage:
        def __init__(self, settings: StageSettings) -> None:
            self.settings = settings

        async def run(self, logger: object, context: StageContext) -> StageResult:
            context.capture("modem.imei", "000123456789012")
            now = datetime.now(UTC)
            return StageResult(self.settings.name, "passed", now, now)

    station = BasicStation(Settings(
        org_id=str(uuid4()), station_id=str(uuid4()), firmware_cache_dir=str(tmp_path / "firmware"),
    ), stage_factories={"probe": CaptureStage})
    result = await station._run_test(OfflinePublisher(), "DUT", str(uuid4()), stages=(StageSettings(name="Read", kind="probe"),))
    assert result.outcome == "passed"
    assert len(station._report_outbox.pending()) == 2
