# Embeint HTF Station

An extensible Python runtime for Embeint Hardware Test Framework (HTF) stations. It subscribes to commands over MQTT, drives programmers and DUTs, and reports logs and results to a compatible HTF server.

The server and web UI are separate private deployments. This repository contains the station runtime, sample stations, and the public MQTT wire contract only.

## Install

```sh
uv add embeint-htf-station
```

For a development checkout:

```sh
uv sync --all-groups
uv run pytest
```

## Compatibility

The MQTT contract is versioned in [`protocol/asyncapi.yaml`](protocol/asyncapi.yaml). Station releases use semantic versioning and are tagged as `vX.Y.Z`.

- Patch releases do not change the wire contract.
- Minor releases may add backwards-compatible fields or messages.
- Major releases may require a compatible server upgrade.

Pin the station version in an HTF workspace with the `west.yml` manifest supplied by the private server repository. Application projects should depend on released package versions through `uv`/PyPI; use an editable dependency only while developing the runtime itself.

## Layout

```
src/embeint_htf_station/
├── cli.py                    # Click entry points
├── config.py                 # Pydantic settings
├── messaging/                # MQTT client and batched logs
├── programmers/              # J-Link, OpenOCD, and nrfutil adapters
├── stages/                   # Reusable stage implementations
├── stations/                 # Station runtimes
└── contracts/                # Generated from protocol/asyncapi.yaml
```

## Samples

```sh
cp samples/basic-station/.env.example samples/basic-station/.env
uv run python samples/basic-station/main.py DUT-001
```

Configuration reads identity and credentials from environment variables, so secrets remain outside source control. Library-provided stages live under `src/embeint_htf_station/stages/`; samples can register their own stage factories.

## Contract generation

The generated MQTT models are committed to the package. Regenerate them after changing the AsyncAPI contract:

```sh
./scripts/gen-mqtt-contracts.sh
git diff --exit-code -- src/embeint_htf_station/contracts
```
