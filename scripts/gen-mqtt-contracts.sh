#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/src/embeint_htf_station/contracts"

uv run --project "$ROOT" python "$ROOT/scripts/generate_contracts.py" python \
  --spec asyncapi \
  --input "$ROOT/protocol/asyncapi.yaml" \
  --output "$OUT/mqtt.py"
