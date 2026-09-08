from uuid import uuid4

from embeint_htf_station.config import Settings
from embeint_htf_station.stations.command_receipts import CommandReceiptStore


def test_command_claim_survives_station_sessions(tmp_path) -> None:
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path / "firmware"),
    )
    command_id = uuid4()

    assert CommandReceiptStore(settings).claim(command_id)
    assert not CommandReceiptStore(settings).claim(command_id)


def test_command_receipts_prune_oldest_claims(tmp_path) -> None:
    settings = Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path / "firmware"),
    )
    store = CommandReceiptStore(settings, max_receipts=2)
    command_ids = [uuid4(), uuid4(), uuid4()]

    for command_id in command_ids:
        assert store.claim(command_id)

    receipts = list((tmp_path / "command-receipts" / "station-1").glob("*.receipt"))
    assert len(receipts) == 2
    assert any(receipt.stem == str(command_ids[-1]) for receipt in receipts)
