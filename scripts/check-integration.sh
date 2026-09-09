#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTEST="$ROOT/server/.venv/bin/pytest"

if [ ! -x "$PYTEST" ]; then
  printf 'integration: server virtual environment is unavailable\n' >&2
  exit 1
fi

printf 'integration: running external-transport contract tests\n'
(
  cd "$ROOT/server"
  "$PYTEST" -q tests/test_redis_message_board.py
)

"$ROOT/scripts/run-multihost-smoke.sh"

case "${MONGARS_RUN_REDIS_INTEGRATION:-0}" in
  1)
    printf 'integration: running live Redis qualification\n'
    "$ROOT/scripts/test-redis-integration.sh"
    ;;
  0)
    printf '%s\n' \
      'integration: live Redis SKIPPED (set MONGARS_RUN_REDIS_INTEGRATION=1)'
    ;;
  *)
    printf 'integration: MONGARS_RUN_REDIS_INTEGRATION must be 0 or 1\n' >&2
    exit 1
    ;;
esac

printf 'integration: completed; physical iPhone validation is not part of this command\n'
