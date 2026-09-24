from __future__ import annotations

import json
from pathlib import Path
from contextlib import contextmanager
from io import BytesIO
from urllib.error import HTTPError

import pytest

from embeint_htf_station.config import Settings, StageSettings, UicrWriteSettings
from embeint_htf_station.stages import id_pool
from embeint_htf_station.stages.id_pool import PoolReservation
from embeint_htf_station.stages.base import StageContext
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage


class Logger:
    async def log(self, level, message):
        pass


def settings():
    return Settings(org_id="org", station_id="station", station_key="station-secret")


def test_station_request_preserves_text_and_sends_dut_and_version(monkeypatch):
    @contextmanager
    def response(request, timeout):
        assert request.get_header("X-station-key") == "station-secret"
        assert json.loads(request.data) == {"dutId": "DUT-1", "variables": ["infuse_id"], "recordVersion": "v2"}
        assert request.full_url.endswith("/api/v1/stations/station/variables/reserve")
        yield BytesIO(b'{"dutId":"DUT-1","values":{"infuse_id":"00001"},"reservationIds":{"infuse_id":"11111111-1111-1111-1111-111111111111"}}')
    monkeypatch.setattr(id_pool, "urlopen", response)
    assert id_pool.allocate_variables(settings(), "DUT-1", ("Infuse_ID",), "v2").values == {"infuse_id": "00001"}


@pytest.mark.parametrize("body", [b'{}', b'{"dutId":"wrong","values":{"id":"1"}}', b'{"dutId":"DUT-1","values":{"id":12}}', b'not-json'])
def test_incomplete_or_wrong_dut_response_is_rejected(monkeypatch, body):
    monkeypatch.setattr(id_pool, "urlopen", lambda *args, **kwargs: BytesIO(body))
    with pytest.raises(id_pool.IdPoolError):
        id_pool.allocate_variables(settings(), "DUT-1", ("id",))


async def test_generic_stage_populates_context_and_exhaustion_does_not_write_outputs(monkeypatch):
    monkeypatch.setattr(id_pool, "allocate_variables", lambda *args: PoolReservation({"infuse_id": "0001", "serial_number": "A1"}, {"infuse_id": "token1", "serial_number": "token2"}))
    stage = id_pool.AllocateVariablesStage(StageSettings(name="IDs", variables=("infuse_id", "serial_number")), settings())
    context = StageContext("DUT-1")
    result = await stage.run(Logger(), context)
    assert result.outcome == "passed"
    assert context.get_output_value("provisioning.infuse_id") == "0001"
    assert context.get_output_value("provisioning.serial_number") == "A1"

    def exhausted(*args):
        raise id_pool.IdPoolError("ID pool exhausted")
    monkeypatch.setattr(id_pool, "allocate_variables", exhausted)
    empty = StageContext("DUT-2")
    assert (await stage.run(Logger(), empty)).outcome == "failed"
    assert not empty.outputs


def test_http_exhaustion_has_clear_error(monkeypatch):
    def exhausted(*args, **kwargs):
        raise HTTPError("https://test", 409, "conflict", {}, None)
    monkeypatch.setattr(id_pool, "urlopen", exhausted)
    with pytest.raises(id_pool.IdPoolError, match="pool exhausted"):
        id_pool.allocate_variables(settings(), "DUT-1", ("id",))


@pytest.mark.parametrize(("pool_value", "expected"), [
    ("00001", 1), ("00009", 9), ("00000", 0), ("1", 1), ("0x00001", 1), ("0x1234", 0x1234),
])
async def test_infuse_uicr_can_use_uploaded_pool_without_remote_infuse_or_hardware_id(
    monkeypatch, tmp_path, pool_value, expected,
):
    calls = []
    def allocate(station, dut, variables, version):
        assert dut == "DUT-1"
        assert variables == ("infuse_id",)
        assert version == "v2"
        return PoolReservation({"infuse_id": pool_value}, {"infuse_id": "token1"})
    monkeypatch.setattr("embeint_htf_station.stages.infuse_provisioning.allocate_variables", allocate)
    async def command(args, logger):
        calls.append(args)
    stage = InfuseProvisioningStage(
        StageSettings(name="Provision", provisioning_source="id_pool", record_version="v2", uicr=(
            UicrWriteSettings(name="infuse_id", value="infuse_id", address=0x1000, width_bits=64),
        )), {}, settings().model_copy(update={"firmware_cache_dir": str(tmp_path / "firmware")}), command,
    )
    context = StageContext("DUT-1")
    assert (await stage.run(Logger(), context)).outcome == "passed"
    assert context.get_output_value("provisioning.infuse_id") == pool_value
    assert len(calls) == 1
    assert calls[0][:4] == ("nrfutil", "device", "program", "--firmware")
    firmware = Path(calls[0][4]).read_text(encoding="ascii")
    assert f":08100000{expected.to_bytes(8, 'little').hex().upper()}" in firmware


def test_yaml_configuration_keeps_pool_fields():
    from embeint_htf_station.config import parse_stage_settings

    stages = parse_stage_settings({"stages": [
        {"name": "Allocate", "kind": "allocate_variables", "variables": ["infuse_id", "serial_number"], "record_version": "v2"},
        {"name": "Provision", "kind": "infuse_provisioning", "provisioning_source": "id_pool", "recordVersion": "v3"},
    ]})
    assert stages[0].variables == ("infuse_id", "serial_number")
    assert stages[0].record_version == "v2"
    assert stages[1].provisioning_source == "id_pool"
    assert stages[1].record_version == "v3"
