from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from embeint_htf_station.config import load_settings_from_yaml
from embeint_htf_station.stages import StageFactory
from embeint_htf_station.stations import BasicStation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the basic HTF station sample.")
    parser.add_argument("dut_id", nargs="?", help="DUT identifier to attach to a direct test run.")
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Listen for server MQTT run-plan commands instead of running one local DUT id.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
        help="Path to the station YAML config.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).with_name(".env"),
        help="Path to the environment file.",
    )
    args = parser.parse_args()

    settings = load_settings_from_yaml(args.config)
    stage_factories: dict[str, StageFactory] = {
        # Register sample-only stages here, for example:
        # "my-stage-kind": MyStage,
    }
    station = BasicStation(settings, stage_factories=stage_factories)
    if args.listen:
        asyncio.run(station.serve_forever())
        return

    if not args.dut_id:
        parser.error("dut_id is required unless --listen is set")

    result = asyncio.run(station.run_once(args.dut_id))
    print(f"test finished: {result.outcome}")


if __name__ == "__main__":
    main()
