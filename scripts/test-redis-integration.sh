#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_BIN="$ROOT/server/.venv/bin"
REDIS_IMAGE="${MONGARS_TEST_REDIS_IMAGE:-redis:7.4-alpine}"
CONTAINER_ENGINE=""
CONTAINER_NAME=""
REMOTE_SSH_HOST="${MONGARS_TEST_REDIS_SSH_HOST:-}"
REMOTE_CONTAINER_PORT=""
TUNNEL_PID=""

case "$REDIS_IMAGE" in
  (*[!A-Za-z0-9._/@:-]*|'')
    printf 'redis-integration: MONGARS_TEST_REDIS_IMAGE is invalid\n' >&2
    exit 1
    ;;
esac

if [ -n "$REMOTE_SSH_HOST" ]; then
  case "$REMOTE_SSH_HOST" in
    (-*|*[!A-Za-z0-9._@:-]*)
      printf 'redis-integration: MONGARS_TEST_REDIS_SSH_HOST is invalid\n' >&2
      exit 1
      ;;
  esac
fi

if [ ! -x "$SERVER_BIN/pytest" ] || [ ! -x "$SERVER_BIN/python" ]; then
  printf 'redis-integration: server virtual environment is unavailable\n' >&2
  exit 1
fi

if ! "$SERVER_BIN/python" -c 'import redis.asyncio' >/dev/null 2>&1; then
  printf 'redis-integration: optional Redis dependency is missing; install server[redis]\n' >&2
  exit 1
fi

cleanup() {
  if [ -n "$TUNNEL_PID" ]; then
    kill "$TUNNEL_PID" >/dev/null 2>&1 || true
    wait "$TUNNEL_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$REMOTE_SSH_HOST" ] && [ -n "$CONTAINER_NAME" ]; then
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_SSH_HOST" \
      "docker rm -f '$CONTAINER_NAME' >/dev/null 2>&1 || true" \
      >/dev/null 2>&1 || true
  fi
  if [ -n "$CONTAINER_ENGINE" ] && [ -n "$CONTAINER_NAME" ]; then
    "$CONTAINER_ENGINE" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if [ -z "${MONGARS_TEST_REDIS_URL:-}" ]; then
  if [ -n "$REMOTE_SSH_HOST" ]; then
    if ! command -v ssh >/dev/null 2>&1; then
      printf 'redis-integration: ssh is required for MONGARS_TEST_REDIS_SSH_HOST\n' >&2
      exit 1
    fi
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_SSH_HOST" \
      'command -v docker >/dev/null'; then
      printf 'redis-integration: Docker is unavailable on the configured SSH host\n' >&2
      exit 1
    fi
  elif command -v docker >/dev/null 2>&1; then
    CONTAINER_ENGINE="docker"
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_ENGINE="podman"
  else
    printf '%s\n' \
      'redis-integration: SKIPPED (set MONGARS_TEST_REDIS_URL, MONGARS_TEST_REDIS_SSH_HOST, or install Docker/Podman)'
    exit 0
  fi

  REDIS_PORT="$($SERVER_BIN/python - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
  REDIS_PASSWORD="$($SERVER_BIN/python - <<'PY'
import secrets

print(secrets.token_hex(24))
PY
)"
  CONTAINER_NAME="mongars-redis-qualification-$$"
  if [ -n "$REMOTE_SSH_HOST" ]; then
    REMOTE_CONTAINER_PORT="$(ssh -o BatchMode=yes -o ConnectTimeout=5 \
      "$REMOTE_SSH_HOST" \
      "python3 -c 'import socket; s=socket.socket(); s.bind((\"127.0.0.1\",0)); print(s.getsockname()[1]); s.close()'")"
    case "$REMOTE_CONTAINER_PORT" in
      (*[!0-9]*|'')
        printf 'redis-integration: remote Docker port discovery failed\n' >&2
        exit 1
        ;;
    esac
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_SSH_HOST" \
      "docker run --detach --rm --name '$CONTAINER_NAME' \
      --publish '127.0.0.1:$REMOTE_CONTAINER_PORT:6379' '$REDIS_IMAGE' \
      redis-server --save '' --appendonly no --protected-mode yes \
      --requirepass '$REDIS_PASSWORD'" >/dev/null
    ssh -N -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
      -L "127.0.0.1:$REDIS_PORT:127.0.0.1:$REMOTE_CONTAINER_PORT" \
      "$REMOTE_SSH_HOST" &
    TUNNEL_PID=$!
  else
    "$CONTAINER_ENGINE" run --detach --rm \
      --name "$CONTAINER_NAME" \
      --publish "127.0.0.1:${REDIS_PORT}:6379" \
      "$REDIS_IMAGE" \
      redis-server --save '' --appendonly no --protected-mode yes \
      --requirepass "$REDIS_PASSWORD" >/dev/null
  fi

  export MONGARS_TEST_REDIS_URL="redis://default:${REDIS_PASSWORD}@127.0.0.1:${REDIS_PORT}/0"
  export MONGARS_TEST_REDIS_WRONG_AUTH_URL="redis://default:wrong-password@127.0.0.1:${REDIS_PORT}/0"

  ready=0
  for _ in $(seq 1 60); do
    if "$SERVER_BIN/python" - <<'PY' >/dev/null 2>&1
import asyncio
import os

from redis.asyncio import Redis


async def check() -> None:
    client = Redis.from_url(os.environ["MONGARS_TEST_REDIS_URL"])
    try:
        await client.ping()
    finally:
        await client.aclose()


asyncio.run(check())
PY
    then
      ready=1
      break
    fi
    sleep 0.1
  done
  if [ "$ready" -ne 1 ]; then
    printf 'redis-integration: authenticated Redis did not become ready\n' >&2
    exit 1
  fi
else
  printf '%s\n' 'redis-integration: using configured Redis endpoint'
fi

printf '%s\n' 'redis-integration: running live auth, outage, recovery, dedupe, timeout, and retention tests'
(
  cd "$ROOT/server"
  "$SERVER_BIN/pytest" -q -m integration tests/test_redis_message_board_integration.py
)
