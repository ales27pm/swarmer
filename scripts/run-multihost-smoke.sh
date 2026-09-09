#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTEST="$ROOT/server/.venv/bin/pytest"

if [ ! -x "$PYTEST" ]; then
  printf 'multihost-smoke: server virtual environment is unavailable\n' >&2
  exit 1
fi

printf '%s\n' \
  'multihost-smoke: topology = one authoritative SQLite control plane + two authenticated worker identities'
printf '%s\n' \
  'multihost-smoke: this bounded protocol harness does not claim shared-SQLite active-active or distinct physical hosts'

(
  cd "$ROOT/server"
  "$PYTEST" -q tests/test_multihost_smoke.py
)

printf '%s\n' \
  'multihost-smoke: protocol failover qualified; physical host placement remains deployment evidence'
