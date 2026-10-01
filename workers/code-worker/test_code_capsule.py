from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_code_worker import install_model_response, job, proposal


@pytest.fixture
def worker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "capsule_code_worker", Path(__file__).with_name("code_worker.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capsule() -> dict[str, Any]:
    return {
        "version": 1,
        "fingerprint": "1" * 64,
        "base_revision_id": None,
        "requirements": [
            {
                "text": "Never send email automatically. Use Canadian French.",
                "source_id": "gmsg_rule",
            }
        ],
    }


def test_old_python_job_preserves_string_interface(worker: ModuleType) -> None:
    claimed = job()
    assert worker.parse_job(claimed) == claimed["payload"]["objective"]


def test_python_capsule_is_delivered_whole_as_json_to_single_model_request(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = job()
    claimed["payload"]["durable_context"] = capsule()
    requests = install_model_response(worker, monkeypatch)

    prompt = worker.parse_job(claimed)
    result = worker.CodeGenerator("http://127.0.0.1:8712", "local-code-model").generate(prompt)

    assert result == proposal()
    assert len(requests) == 1
    body = json.loads(requests[0].data)
    assert json.loads(body["messages"][1]["content"]) == claimed["payload"]
    assert body["max_tokens"] >= 1_024
    prompt_bytes = len(
        json.dumps(
            body["messages"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    )
    schema_bytes = len(
        json.dumps(
            worker.RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    )
    assert prompt_bytes + schema_bytes + body["max_tokens"] + 512 <= 8_192


def test_large_capsule_fails_before_any_model_post_without_truncating(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = job()
    claimed["payload"]["durable_context"] = capsule()
    claimed["payload"]["durable_context"]["requirements"][0]["text"] = (
        "Preserve this instruction. " * 600 + "Final constraint."
    )
    requests = install_model_response(worker, monkeypatch)
    prompt = worker.parse_job(claimed)

    with pytest.raises(worker.GenerationError, match="context.*budget"):
        worker.CodeGenerator("http://127.0.0.1:8712", "local-code-model").generate(prompt)

    assert requests == []
    assert json.loads(prompt)["durable_context"] == claimed["payload"]["durable_context"]


def test_python_capsule_validator_is_same_shipped_contract_as_server() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (
        Path(__file__).with_name("agent_capsule.py").read_bytes()
        == (root / "server/app/services/agent_capsule.py").read_bytes()
    )


@pytest.mark.parametrize("value", [None, {}, {"capability_request": {}}])
def test_malformed_capsule_never_reaches_model(worker: ModuleType, value: object) -> None:
    claimed = job()
    claimed["payload"]["durable_context"] = value
    with pytest.raises(worker.GenerationError):
        worker.parse_job(claimed)
