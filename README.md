# station/

Python 3.14 runtime for the HTF station. Subscribes to commands from the server over MQTT, drives the programmer and DUT, and streams logs back.

## Layout

```
src/embeint_htf_station/
├── cli.py                    # click entry points
├── config.py                 # pydantic-settings
├── messaging/
│   ├── client.py             # aiomqtt wrapper + heartbeat
│   └── batch_logger.py       # 500ms / 4KB log batching (v1 lesson)
├── programmers/              # J-Link, OpenOCD, nrfutil adapters
├── runners/                  # Test plan execution
├── stages/                   # Reusable stage implementations
└── contracts/                # Generated from ../proto/ (run ../proto/scripts/gen-python.sh)
```

## Run

```sh
cd station
uv sync
HTF_ORG_ID=... HTF_STATION_ID=... uv run htf-station run
```

## Basic station sample

The basic station sample is a pipeline smoke test. It accepts a DUT id, connects
to MQTT using the existing station contract, runs the configured stage list,
logs the run, and finishes with a local `passed` result.

```sh
cd station
cp samples/basic-station/.env.example samples/basic-station/.env
uv run python samples/basic-station/main.py DUT-001
```

To run it from the web kiosk flow, start the sample in MQTT listen mode:

```sh
cd station
uv run python samples/basic-station/main.py --listen
```

Configuration lives in `samples/basic-station/config.yaml`. MQTT and station
identity values are referenced as environment variables so secrets and station
keys do not need to be committed. A sibling `.env` file is loaded automatically
when present, with already-exported environment variables taking priority.
Stages are configured in the same YAML file. The default sample stage prints
`testing`, waits five seconds, and reports the stage as passed.

Library-provided stages live under `src/embeint_htf_station/stages/`, with each
stage in its own file. Sample-specific stages can be registered from
`samples/basic-station/main.py` by passing a `stage_factories` mapping into
`BasicStation`.

## Test

```sh
uv run pytest
```
