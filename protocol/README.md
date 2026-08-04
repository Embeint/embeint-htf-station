# HTF station MQTT contract

[`asyncapi.yaml`](asyncapi.yaml) is the public, versioned wire contract between an HTF station and an HTF server. Its `info.version` is the protocol version.

Keep additions backwards compatible within a major protocol version. Generate and commit the Python models with `../scripts/gen-mqtt-contracts.sh` whenever this file changes.
