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
└── contracts/                # Generated from ../proto/ (run ../proto/scripts/gen-python.sh)
```

## Run

```sh
cd station
uv sync
HTF_ORG_ID=... HTF_STATION_ID=... uv run htf-station run
```

## Test

```sh
uv run pytest
```
