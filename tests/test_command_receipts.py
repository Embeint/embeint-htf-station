import json
from uuid import uuid4

from embeint_htf_station.config import Settings
from embeint_htf_station.contracts.mqtt import Command
from embeint_htf_station.stations.basic import BasicStation
from embeint_htf_station.stations.command_receipts import CommandReceiptStore


class Publisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.messages.append((topic, json.loads(payload)))


def settings(tmp_path) -> Settings:
    return Settings(
        org_id="org-1",
        station_id="station-1",
        firmware_cache_dir=str(tmp_path / "firmware"),
    )


def run_command() -> Command:
    return Command(
        id=uuid4(),
        kind="run-plan",
        payload={"runId": str(uuid4()), "dutId": "DUT-001", "lane": "default"},
    )


def test_command_claim_survives_station_sessions(tmp_path) -> None:
    station_settings = settings(tmp_path)
    command = run_command()

    assert CommandReceiptStore(station_settings).claim(command)
    assert not CommandReceiptStore(station_settings).claim(command)


def test_command_receipt_tracks_unfinished_runs_until_all_are_terminal(tmp_path) -> None:
    store = CommandReceiptStore(settings(tmp_path))
    left_run_id, right_run_id = str(uuid4()), str(uuid4())
    command = Command(
        id=uuid4(),
        kind="run-batch",
        payload={
            "runs": [
                {"runId": left_run_id, "dutId": "LEFT", "lane": "left"},
                {"runId": right_run_id, "dutId": "RIGHT", "lane": "right"},
            ],
        },
    )

    assert store.claim(command)
    assert {run.run_id for run in store.incomplete()[0].runs} == {left_run_id, right_run_id}

    store.complete_run(command.id, left_run_id)
    assert [run.run_id for run in store.incomplete()[0].runs] == [right_run_id]

    store.complete_run(command.id, right_run_id)
    assert store.incomplete() == ()


async def test_restart_reports_claimed_command_as_interrupted_without_executing_it(tmp_path) -> None:
    station_settings = settings(tmp_path)
    run_ids = [str(uuid4()), str(uuid4())]
    command = Command(
        id=uuid4(),
        kind="run-batch",
        payload={
            "runs": [
                {"runId": run_ids[0], "dutId": "LEFT", "lane": "left"},
                {"runId": run_ids[1], "dutId": "RIGHT", "lane": "right"},
            ],
        },
    )
    first_session = BasicStation(station_settings)
    assert first_session._command_receipts.claim(command)

    publisher = Publisher()
    restarted = BasicStation(station_settings)
    await restarted._recover_interrupted_commands(publisher)

    results = [payload for topic, payload in publisher.messages if topic.endswith("/result")]
    assert [(result["runId"], result["outcome"]) for result in results] == [
        (run_ids[0], "error"),
        (run_ids[1], "error"),
    ]
    assert restarted._command_receipts.incomplete() == ()
    assert not restarted._command_receipts.claim(command)


def test_command_receipts_prune_oldest_completed_claims(tmp_path) -> None:
    store = CommandReceiptStore(settings(tmp_path), max_receipts=2)
    commands = [run_command(), run_command(), run_command()]

    for command in commands:
        assert store.claim(command)
        store.complete(command.id)

    receipts = list((tmp_path / "command-receipts" / "station-1").glob("*.receipt"))
    assert len(receipts) == 2
    assert any(receipt.stem == str(commands[-1].id) for receipt in receipts)
