#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -d "$ROOT/mobile/node_modules" ]; then
  (
    cd "$ROOT/mobile"
    npm run typecheck
    npm run lint
    npm test -- --runInBand
    npx expo-doctor
  )
else
  echo "mobile/node_modules missing — run npm install first" >&2
fi

if [ -d "$ROOT/server/.venv" ]; then
  (
    cd "$ROOT/server"
    source .venv/bin/activate
    ruff format --check .
    ruff check .
    mypy app
    pytest -q
    bandit -r app
  )
else
  echo "server/.venv missing — create the virtualenv first" >&2
fi
