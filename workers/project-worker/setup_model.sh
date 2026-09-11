#!/usr/bin/env bash
set -euo pipefail
TASK_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Verify the already-installed official model; never select different weights
# silently or alter planner/evaluator configuration.
python3 - <<'PY'
import json
import urllib.request

model = "qwen3-coder:30b-a3b-q4_K_M"
expected = "06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:11434/api/tags", timeout=10) as response:
    models = json.load(response)["models"]
if not any(item.get("name") == model and item.get("digest") == expected for item in models):
    raise SystemExit("The required verified Qwen3-Coder model is missing or has a different digest.")
PY
OLLAMA_HOST=127.0.0.1:11434 ollama create swarmer-project-qwen3-coder:30b-32k-06c1097e -f "$TASK_DIRECTORY/Modelfile"
python3 - <<'PY'
import json
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:11434/api/tags", timeout=10) as response:
    models = json.load(response)["models"]
candidate = next(item for item in models if item.get("name") == "swarmer-project-qwen3-coder:30b-32k-06c1097e")
print(json.dumps({"model": candidate["name"], "digest": candidate["digest"]}))
PY
