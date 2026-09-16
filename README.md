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

## Station credentials

Create a station from the HTF operator UI and save the one-time `.env` output. Each station has its own MQTT username/password and API key:

```sh
HTF_MQTT_USERNAME=station-...
HTF_MQTT_PASSWORD=st_mqtt_...
HTF_API_KEY=st_api_...
```

Never reuse these values between stations. MQTT credentials are restricted by the broker to the station's own heartbeat, stage, log, and result topics, plus its command subscription. Rotate a credential in the operator UI after exposure; revoked credentials cannot reconnect or access station APIs.

### MQTT session takeover detection

The broker requires the provisioned MQTT username as the station's MQTT client ID. MQTT permits only one active connection for a client ID, so another connection using the same provisioned credentials will disconnect the active station session. This can be caused by a network or broker fault as well as credential reuse; MQTT does not expose enough information to distinguish them reliably.

An established session that ends unexpectedly emits a `broker.disconnected` warning with `possible_session_takeover=true`. Treat that event as a credential-exposure signal: investigate the broker and network logs, then rotate the station credential in the operator UI when reuse cannot be ruled out.

## Contract generation

The generated MQTT models are committed to the package. Regenerate them after changing the AsyncAPI contract:

```sh
./scripts/gen-mqtt-contracts.sh
git diff --exit-code -- src/embeint_htf_station/contracts
```

## MQTT TLS and station certificates

New configurations default to verified TLS on port 8883. The production endpoint
is `mqtt.app.embeint-htf.com:8883` and requires a unique station client certificate
**and** the station MQTT username/password. Download the one-time provisioning
bundle from the HTF station setup page. Keep its private key readable only by the
station service account (for example `chmod 600 client.key`). Never commit it.

```yaml
mqtt:
  transport: mtls
  host: mqtt.app.embeint-htf.com
  port: 8883
  client_cert: ./credentials/client.pem
  client_key: ./credentials/client.key
  # Optional custom broker trust; omit to use the system public CA store.
  # ca_cert: ./credentials/broker-ca.pem
station:
  org_id: ${HTF_ORG_ID}
  station_id: ${HTF_STATION_ID}
```

`HTF_MQTT_TRANSPORT`, `HTF_MQTT_HOST`, `HTF_MQTT_PORT`, `HTF_MQTT_USERNAME`,
`HTF_MQTT_PASSWORD`, `HTF_MQTT_CA_CERT`, `HTF_MQTT_CLIENT_CERT`, and
`HTF_MQTT_CLIENT_KEY` override their YAML values. Adjacent `.env` values fill
missing process environment variables. Certificate paths in YAML or its overrides
resolve relative to the YAML file. With the environment-only CLI, paths resolve
relative to the working directory. Legacy `HTF_BROKER_*` names remain supported
by the environment-only CLI; prefer `HTF_MQTT_*` for new installations.

Modes are `tls` (verified server TLS, optional paired client certificate/key),
`mtls` (requires both client files), and `plaintext` (explicit development only).
Local samples select plaintext; set both transport and port when switching an
existing sample to production. For an environment-only local development run,
set `HTF_MQTT_TRANSPORT=plaintext` and `HTF_MQTT_PORT=1883`.
There is no insecure TLS mode. A client trust CA issued for stations is not
necessarily the CA that signed the broker certificate.

Missing/unreadable PEM files and mismatched/encrypted keys stop startup before
MQTT connects. TLS handshake failure prevents the station from accepting runs.
For rejected connections check the broker hostname, system clock, broker CA
chain, client chain/expiry and MQTT credentials. Do not share keys or passwords
in support logs. To rotate, replace the certificate/key pair atomically while
the station is stopped, restart to load them, verify a heartbeat, then revoke the
old certificate. Existing contexts are not changed by overwriting files.

Run `uv run pytest` for unit tests. Run
`HTF_TLS_INTEGRATION=1 uv run pytest tests/test_tls_integration.py` with Docker
available for real Mosquitto mTLS tests (also required by CI).

A complete production transport sample is in `samples/production-mtls/config.yaml`.
After installing the credentials and setting station environment variables, run
`uv run python samples/basic-station/main.py --listen --config samples/production-mtls/config.yaml`.
