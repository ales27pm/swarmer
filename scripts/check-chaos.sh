#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTEST="$ROOT/server/.venv/bin/pytest"
JEST="$ROOT/mobile/node_modules/.bin/jest"

if [ ! -x "$PYTEST" ]; then
  printf 'chaos: server virtual environment is unavailable\n' >&2
  exit 1
fi
if [ ! -x "$JEST" ]; then
  printf 'chaos: mobile Jest installation is unavailable\n' >&2
  exit 1
fi

printf 'chaos: running bounded control-plane failure scenarios\n'
(
  cd "$ROOT/server"
  "$PYTEST" -q \
    tests/test_v011_chaos.py \
    tests/test_multihost_smoke.py \
    tests/test_concurrency_stress.py \
    tests/test_outbox_concurrency.py \
    tests/test_outbox_publication_leases.py \
    tests/test_maintenance_leases.py \
    tests/test_agent_reaper.py
)

printf 'chaos: running mobile offline/restart replay scenarios\n'
(
  cd "$ROOT/mobile"
  "$JEST" --runInBand src/lib/state/mutation-outbox.test.ts
)

printf '%s\n' \
  'chaos: passed bounded automated scenarios; live Redis and physical-device failures are separate qualification levels'
