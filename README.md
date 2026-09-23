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

### Automatic renewal

Updated HTF servers support station-owned automatic certificate renewal. It is
enabled by default for `mtls`; set `HTF_MQTT_AUTO_RENEW=false` to opt out. Keep
`HTF_API_KEY` and a verified HTTPS `HTF_API_BASE_URL` configured. The station
does not need an OpenBao token. Initial certificate installation is still manual.

The runtime checks hourly while idle and renews seven days before expiry (or in
the last third of a shorter certificate lifetime). Active and queued jobs defer
renewal, so allow an idle interval before expiry. It generates a fresh private
key locally, proves possession of its current key, and requests a replacement
through the HTF API. Network failures retry with bounded backoff without
discarding the current pair. Revoked/expired certificates require admin recovery.

The certificate's parent directory must be writable by the station account.
The private `.htf-mtls-<station UUID>` directory stores pending requests and
versioned key/certificate pairs, using owner-only permissions on POSIX. Keep
this directory persistent across restarts, private, and out of Git/support logs.
An atomic pointer activates the replacement and MQTT reconnects between jobs.
The server retires the old certificate after the overlap (normally 24 hours).
Original enrollment files are preserved. For manual re-enrollment, stop the
station and securely archive the old renewal directory before installing the
new bundle. Do not run two station processes with the same identity.

Look for `station.certificate.renewed` or `station.certificate.renewal_failed` in
station logs. The GUI shows the new certificate's expiry and the old one's
retirement time. Older server deployments will reject renewal until upgraded;
do not assume installing only the station update enables end-to-end renewal.

Run `uv run pytest` for unit tests. Run
`HTF_TLS_INTEGRATION=1 uv run pytest tests/test_tls_integration.py` with Docker
available for real Mosquitto mTLS tests (also required by CI).

A complete production transport sample is in `samples/production-mtls/config.yaml`.
After installing the credentials and setting station environment variables, run
`uv run python samples/basic-station/main.py --listen --config samples/production-mtls/config.yaml`.

## Project ID pools

Project variables use uploaded CSV pools. Configure `HTF_API_KEY` and
`HTF_API_BASE_URL`, then reserve values, use them, and explicitly commit after
all required work succeeds:

```yaml
stages:
  - name: Reserve IDs
    kind: reserve_variables
    variables: [infuse_id, serial_number]
    record_version: v2

  - name: Show ID
    kind: print
    message: 'ID=${provisioning.infuse_id}'
    wait_seconds: 0

  - name: Register with external service
    kind: http_request
    http:
      url: https://manufacturing.example.com/devices
      method: POST
      headers:
        Authorization: 'Bearer ${MANUFACTURING_TOKEN}'
      json:
        dut_id: '${dut_id}'
        infuse_id: '${provisioning.infuse_id}'
        serial_number: '${provisioning.serial_number}'
      timeout_seconds: 30
      expected_statuses: [200, 201]
      outputs:
        external.receipt: receipt.id

  - name: Program reserved ID
    kind: infuse_provisioning
    provisioning_source: id_pool
    programmer: jlink_1
    uicr:
      - name: infuse_id
        source: context
        value: provisioning.infuse_id
        bytes: 8
        endian: LSB

  - name: Commit IDs
    kind: commit_variables
    variables: [infuse_id, serial_number]
```

Replace the example service URL and response mapping with your service contract.
`jlink_1` must be a local programmer with a target device that defines its UICR
address; alternatively supply an explicit UICR address. Hardware programming is
optional: use any required stages between reservation and commit. The same stage
syntax works inside named lane plans.

`reserve_variables` (also available as `allocate_variables`) **only reserves**.
Reservations are exclusive to the project/DUT/variable and never expire or
release automatically. The server handles concurrent stations atomically.
Re-requesting the same DUT returns its reserved or committed values, even if the
pool is otherwise empty. Additional variables in a later configuration version
are reserved without replacing existing committed values. Values stay strings,
including leading zeros.

Reservation writes `provisioning.<variable>` and `reservation.<variable>` into
this run's context. `${provisioning.infuse_id}`, `${external.receipt}`, `${dut_id}`,
and `${run_id}` can be used in string settings, including nested HTTP JSON and
headers. `${context.provisioning.infuse_id}` is an equivalent explicit context
reference. References resolve once immediately before each stage; a missing
reference fails the stage. Stage names, kinds, programmer routing, variable
lists, locks and dependency declarations remain static. Numeric configuration
fields such as timeout and width must be literal numbers. Uppercase `${ENV_VAR}`
is the existing configuration-time environment expansion; runtime values use
lowercase/dotted names. Python stages can still use
`context.get_output_value("provisioning.infuse_id")`.

`http_request` supports GET/POST/PUT/PATCH/DELETE, explicit headers, optional JSON,
timeout up to 120 seconds, expected status codes, and response outputs mapped
from dotted JSON paths (including array indexes). Outputs must be strings or
integers; response JSON is limited to 1 MiB when extracting outputs. The station
credential is never automatically sent to an external service. Requests do not
follow redirects or retry automatically. The stage supplies an `Idempotency-Key`
derived from DUT, stage name and reservation IDs unless you provide one. It is
stable across retries for the same reservations, and changes after explicit
release/re-reservation. **Your service must implement idempotency** for this to
prevent duplicate side effects; keep the stage name, request and reserved
variable set stable when retrying.

A timeout may mean the external service consumed the ID. The run fails and keeps
the reservation. Reconcile the external outcome before rerunning side effects,
committing, or releasing; never release just because a request timed out. After
confirmed external success, an operator can use an approved recovery plan that
reserves the same DUT, verifies completion and commits without repeating that
side effect. Commit timeouts can be retried with the same reservation IDs.

Put `commit_variables` after every required operation. It commits exactly the
listed variables using their reservation IDs and is retry-safe. A missing,
released or replaced reservation is rejected, and a repeated commit preserves
the original timestamp/station. Plans containing a commit stop after any failed
stage. Re-reserving a variable within one run must return the same token and
value; a changed pair fails the run before it can be committed. Commit also
rejects failed prerequisites and changed context values. Every stage in a plan
that reserves IDs checks its declared dependencies, including standalone runs.
Same-lane dependencies use stages already completed in this run. A cross-lane
dependency without a batch result fails before that stage runs, so registration
cannot proceed without its verification prerequisite. Every dependency in a
reservation plan must declare and reach `passed`; a declared `failed` outcome
cannot authorize an external side effect or commit.
Only declared cross-lane dependencies are checked; independent lanes keep their
own contexts.

Admin release in the project's **DUT records & IDs** history is the only way to
make a reserved or committed value reusable. History retains reservation,
commit and release attribution. No failure, abort, disconnect or reimport clears
an ID.

`infuse_provisioning` with `provisioning_source: id_pool` can also reserve its
requested keys directly, but still needs a subsequent `commit_variables` stage.
Explicit `provisioning.hardware_id` references select the pool even when a chip
hardware ID exists. Context-only/literal UICR writes make no allocation request.
The default `infuse_api` retains the existing Infuse workflow; it is a separate
data source and does not automatically import legacy IDs into project pools.

Deploy the companion server and its reservation migration before this station
change, and update pool plans to include explicit commit. Existing permanent
assignments remain committed during migration. This change does not publish or
deploy automatically.

## License

Licensed under the Functional Source License, Version 1.1, ALv2 Future License
(FSL-1.1-ALv2). See [LICENSE.md](LICENSE.md) for the full terms.
