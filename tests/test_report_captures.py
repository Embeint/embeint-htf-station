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
    with pytest.raises(ValueError, match="1024 per run"):
        context.capture("custom.extra", "0001")


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
