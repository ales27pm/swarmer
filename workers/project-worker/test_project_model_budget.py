"""Explicit context settings must reach both source selection and the real request."""

import json

import project_worker as worker
import pytest


class RequestCaptured(Exception):
    pass


def payload():
    return {
        "objective": "Keep the existing application behavior.",
        "conversation": [],
        "files": [
            {"path": "app.py", "content": "# " + "é" * 13_000 + "\nTAIL_MARKER = 1\n"}
        ],
        "plan": [],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
    }


def capture(monkeypatch, generator, data):
    requests = []

    class Opener:
        def open(self, request, **_kwargs):
            requests.append(json.loads(request.data))
            raise RequestCaptured

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RequestCaptured):
        generator.generate(data)
    assert len(requests) == 1
    return requests[0]


def test_64k_retains_complete_source_above_old_prompt_limit_and_posts_context(
    monkeypatch,
):
    data = payload()
    old = worker.model_context(data)
    assert old["selected_complete_files"] == []
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "qwen3-coder:30b",
        context_tokens=64_000,
        prompt_max_bytes=50_000,
    )
    body = capture(monkeypatch, generator, data)
    size = sum(len(message["content"].encode()) for message in body["messages"])
    assert 22_000 < size <= 50_000
    assert data["files"][0]["content"] in body["messages"][-1]["content"]
    assert generator.last_visible_paths == {"app.py"}
    assert body["options"]["num_ctx"] == 64_000
    assert body["options"]["num_predict"] == 2_000


def test_default_context_and_prompt_unchanged(monkeypatch):
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "local-model")
    body = capture(monkeypatch, generator, payload())
    assert body["options"]["num_ctx"] == 32_768
    assert body["options"]["num_predict"] == 2_000
    assert sum(len(m["content"].encode()) for m in body["messages"]) <= 22_000


@pytest.mark.parametrize(
    "context,prompt",
    [
        (True, 22_000),
        (64_000.0, 50_000),
        (float("nan"), 22_000),
        (float("inf"), 22_000),
        (0, 22_000),
        (64_001, 50_000),
        (32_767, 22_000),
        (64_000, True),
        (64_000, 9_999),
        (32_768, 30_000),
        (64_000, 60_977),
    ],
)
def test_invalid_combination_rejected_before_request(context, prompt, monkeypatch):
    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *_a: pytest.fail("no request")
    )
    with pytest.raises(ValueError):
        worker.ProjectGenerator(
            "http://127.0.0.1:11434/v1",
            "local-model",
            context_tokens=context,
            prompt_max_bytes=prompt,
        )


def test_context_budget_exact_reserve_boundary():
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "local-model",
        context_tokens=64_000,
        prompt_max_bytes=60_976,
    )
    assert generator.prompt_max_bytes + 2_000 + 1_024 == generator.context_tokens


def test_environment_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MONGARS_PROJECT_PROMPT_MAX_BYTES", raising=False)
    assert worker.model_budgets_from_environment() == (32_768, 22_000)
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", "64000")
    monkeypatch.setenv("MONGARS_PROJECT_PROMPT_MAX_BYTES", "50000")
    assert worker.model_budgets_from_environment() == (64_000, 50_000)


@pytest.mark.parametrize(
    "bad", ["", "NaN", "inf", "true", "64_000", "64000.0", " 64000"]
)
def test_malformed_context_environment_rejected(monkeypatch, bad):
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", bad)
    with pytest.raises(ValueError):
        worker.model_budgets_from_environment()


def test_compact_repair_can_keep_required_capsule_above_old_budget(monkeypatch):
    data = payload()
    data["files"] = [{"path": "app.py", "content": "VALUE = 1\n"}]
    data["conversation"] = [
        {"role": "user", "content": "Fix the existing test failure."},
        {"role": "assistant", "content": worker.MODEL_TIMEOUT_DIAGNOSTIC},
    ]
    data["checks"] = [
        {
            "command": ["python", "-m", "pytest", "-q"],
            "status": "failed",
            "exit_code": 1,
            "output": "AssertionError in app.py",
            "duration_ms": 1,
        }
    ]
    requirements = [{"source_id": f"msg_{i}", "text": str(i) * 8_000} for i in range(3)]
    data["durable_context"] = {
        "version": 1,
        "fingerprint": "a" * 64,
        "base_revision_id": None,
        "requirements": requirements,
    }
    generator = worker.ProjectGenerator(
        "http://127.0.0.1:11434/v1",
        "qwen3-coder:30b",
        context_tokens=64_000,
        prompt_max_bytes=50_000,
    )
    body = capture(monkeypatch, generator, data)
    size = sum(len(m["content"].encode()) for m in body["messages"])
    assert 22_000 < size <= 50_000
    for requirement in requirements:
        assert requirement["text"] in body["messages"][-1]["content"]
    assert body["options"]["num_ctx"] == 64_000
    assert body["options"]["num_predict"] == 512


def test_main_passes_both_environment_budgets_to_generator(monkeypatch):
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", "64000")
    monkeypatch.setenv("MONGARS_PROJECT_PROMPT_MAX_BYTES", "50000")
    for name in (
        "MONGARS_PROJECT_RUNTIME_IMAGE",
        "MONGARS_PROJECT_MODEL_URL",
        "MONGARS_PROJECT_MODEL_ID",
        "MONGARS_SERVER_URL",
        "MONGARS_AGENT_ID",
        "MONGARS_AGENT_CREDENTIAL",
    ):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setattr("sys.argv", ["project_worker.py", "--once"])
    captured = []

    class Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        def probe_profile(self):
            return {"status": "unknown"}

    monkeypatch.setattr(worker, "DockerRunner", Runner)
    monkeypatch.setattr(
        worker, "ProjectGenerator", lambda *_args, **kwargs: captured.append(kwargs)
    )
    monkeypatch.setattr(worker, "run_once", lambda *_args: False)
    worker.main()
    assert captured[0]["context_tokens"] == 64_000
    assert captured[0]["prompt_max_bytes"] == 50_000


def test_main_invalid_pair_fails_before_runtime_probe(monkeypatch):
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", "32768")
    monkeypatch.setenv("MONGARS_PROJECT_PROMPT_MAX_BYTES", "50000")
    monkeypatch.setattr("sys.argv", ["project_worker.py", "--once"])
    monkeypatch.setattr(
        worker, "DockerRunner", lambda *_a, **_k: pytest.fail("no runtime probe")
    )
    with pytest.raises(ValueError, match="cover prompt"):
        worker.main()


@pytest.mark.parametrize(
    "bad", ["", "NaN", "inf", "false", "50_000", "50000.0", " 50000"]
)
def test_malformed_prompt_environment_rejected(monkeypatch, bad):
    monkeypatch.setenv("MONGARS_PROJECT_MODEL_CONTEXT_TOKENS", "64000")
    monkeypatch.setenv("MONGARS_PROJECT_PROMPT_MAX_BYTES", bad)
    with pytest.raises(ValueError):
        worker.model_budgets_from_environment()
