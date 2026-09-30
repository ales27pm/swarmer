from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
from test_text_worker import FakeConnection, draft, payload, stream
from test_text_worker import worker as worker  # noqa: PLC0414


@pytest.mark.parametrize(
    "model", ["qwen3:7b", "installed:g9v3", "installed:hermes3", "installed:qwen2.5"]
)
def test_bounded_draft_disables_internal_thinking_independently_of_alias(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    generator = worker.TextGenerator("http://127.0.0.1:11434", model, timeout_seconds=1)
    connection = FakeConnection(stream())
    monkeypatch.setattr(generator, "_connection", lambda: connection)
    assert generator.generate(payload(), ensure_active=lambda: None) == draft()
    calls = [call for call in connection.calls if call[0] == "POST"]
    assert len(calls) == 1
    assert calls[0][2]["think"] is False


@pytest.mark.parametrize("layers", [0, 28, 128])
def test_operator_gpu_setting_reaches_the_single_model_call(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, layers: int
) -> None:
    generator = worker.TextGenerator(
        "http://127.0.0.1:11434", "installed:3b", timeout_seconds=1, gpu_layers=layers
    )
    connection = FakeConnection(stream())
    monkeypatch.setattr(generator, "_connection", lambda: connection)
    assert generator.generate(payload(), ensure_active=lambda: None) == draft()
    calls = [call for call in connection.calls if call[0] == "POST"]
    assert len(calls) == 1
    assert calls[0][2]["options"] == {
        "temperature": 0,
        "num_predict": 2912,
        "num_gpu": layers,
    }
    assert connection.initial_sock.closed.is_set()


@pytest.mark.parametrize("layers", [-1, 129, True, False, 1.5, "28", None])
def test_gpu_configuration_rejects_unbounded_or_coerced_values(
    worker: ModuleType, layers: object
) -> None:
    with pytest.raises(ValueError, match="GPU layers"):
        worker.TextGenerator("http://127.0.0.1:11434", "installed:3b", gpu_layers=layers)


@pytest.mark.parametrize("configured,expected", [(None, 0), ("0", 0), ("28", 28)])
def test_startup_reads_gpu_configuration_from_operator_environment(
    worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: int,
) -> None:
    monkeypatch.setattr("sys.argv", ["text_worker.py", "--once"])
    monkeypatch.setenv("MONGARS_TEXT_MODEL_ID", "installed:3b")
    monkeypatch.setenv("MONGARS_SERVER_URL", "http://127.0.0.1:8710")
    monkeypatch.setenv("MONGARS_AGENT_ID", "agent")
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "credential")
    if configured is None:
        monkeypatch.delenv("MONGARS_TEXT_GPU_LAYERS", raising=False)
    else:
        monkeypatch.setenv("MONGARS_TEXT_GPU_LAYERS", configured)
    observed = []

    def run(base: str, agent: str, credential: str, generator: Any, **kwargs: Any) -> bool:
        observed.append(generator.gpu_layers)
        return False

    monkeypatch.setattr(worker, "run_once", run)
    worker.main()
    assert observed == [expected]


@pytest.mark.parametrize("configured", ["-1", "129", "3.5", "NaN", "auto", ""])
def test_invalid_gpu_environment_fails_before_claiming_a_job(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setattr("sys.argv", ["text_worker.py", "--once"])
    monkeypatch.setenv("MONGARS_TEXT_MODEL_ID", "installed:3b")
    monkeypatch.setenv("MONGARS_TEXT_GPU_LAYERS", configured)
    monkeypatch.setenv("MONGARS_SERVER_URL", "http://127.0.0.1:8710")
    monkeypatch.setenv("MONGARS_AGENT_ID", "agent")
    monkeypatch.setenv("MONGARS_AGENT_CREDENTIAL", "credential")
    monkeypatch.setattr(worker, "run_once", lambda *a, **k: pytest.fail("must not claim a job"))
    with pytest.raises(ValueError):
        worker.main()


@pytest.mark.parametrize("field", ["gpu_layers", "options"])
def test_job_cannot_override_operator_compute_placement(worker: ModuleType, field: str) -> None:
    with pytest.raises(worker.GenerationError):
        worker.validate_payload({**payload(), field: 28})
