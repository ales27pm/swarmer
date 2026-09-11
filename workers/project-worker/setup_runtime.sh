#!/usr/bin/env bash
set -euo pipefail
TASK_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
docker build --pull=false --tag swarmer-project-runtime:1 "$TASK_DIRECTORY"
TASK_IMAGE_ID="$(docker image inspect --format '{{.Id}}' swarmer-project-runtime:1)"
[[ "$TASK_IMAGE_ID" =~ ^sha256:[a-f0-9]{64}$ ]]
printf 'MONGARS_PROJECT_RUNTIME_IMAGE=%s\n' "$TASK_IMAGE_ID"
