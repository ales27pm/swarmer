"""Exercise admission against real read-only SQLite and bounded Ollama replies."""
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner
from runner import BusyError, DockerRunner, OllamaProbeError, Settings
from studio import create_app


@pytest.fixture
def admission(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "state", tmp_path / "models", tmp_path / "runtime",
                        tmp_path / "db", gpu_lock=tmp_path / "gpu.lock")
    with sqlite3.connect(settings.db) as db:
        db.execute("CREATE TABLE goal_model_calls(provider_source TEXT, status TEXT, lease_expires_at TEXT)")
        db.execute("CREATE TABLE agent_jobs(required_skill TEXT, status TEXT, lease_expires_at TEXT)")
    settings.runtime.mkdir()
    executable = settings.runtime / "sd-cli"
    executable.write_text("fixture executable")
    executable.chmod(0o700)
    settings.models.mkdir()
    (settings.models / "model.bin").write_bytes(b"model")
    monkeypatch.setattr(runner, "MODEL_FILES", {"fixture": ("model.bin", 5)})
    reply = {"body": b'{"models":[]}'}
    probes = []

    def urlopen(url, *, timeout):
        probes.append((url, timeout))
        if "error" in reply:
            raise reply["error"]
        return io.BytesIO(reply["body"])

    monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)
    engine = DockerRunner(settings)
    monkeypatch.setattr(engine, "cleanup_interrupted", lambda: None)
    return engine, reply, probes


@pytest.mark.parametrize("models", [[], [{"size_vram": 0}], [{"size_vram": 0}, {"size_vram": 0}]])
def test_only_explicit_cpu_residency_is_admitted(admission, models):
    engine, reply, probes = admission
    reply["body"] = json.dumps({"models": models}).encode()
    assert engine.admission() is None
    assert engine.ready()[::2] == (True, "ready")
    assert probes == [(engine.settings.ollama, 3)] * 2


@pytest.mark.parametrize("models", [[{"size_vram": 1}], [{"size_vram": 0}, {"size_vram": 6442450944}]])
def test_any_positive_gpu_residency_reserves_gpu(admission, models):
    engine, reply, _ = admission
    reply["body"] = json.dumps({"models": models}).encode()
    with pytest.raises(BusyError) as result:
        engine.admission()
    assert result.value.code == "ollama_gpu_reserved"
    ready, message, code = engine.ready()
    assert not ready and code == "ollama_gpu_reserved"
    assert "Ollama" in message and "mémoire GPU" in message


@pytest.mark.parametrize("payload", [
    None, [], {}, {"models": None}, {"models": {}},
    {"models": [None]}, {"models": [{}]}, {"models": [{"size_vram": None}]},
    {"models": [{"size_vram": False}]}, {"models": [{"size_vram": -1}]},
    {"models": [{"size_vram": 0.0}]}, {"models": [{"size_vram": "0"}]},
    {"models": [{"size_vram": 0}, {}]}, {"models": [{"size_vram": 1}, {}]},
])
def test_unknown_vram_never_means_cpu_or_free(admission, payload):
    engine, reply, _ = admission
    reply["body"] = json.dumps(payload).encode()
    with pytest.raises(OllamaProbeError):
        engine.admission()
    assert engine.ready()[::2] == (False, "ollama_unavailable")


@pytest.mark.parametrize("failure", ["invalid_json", "oversized", "network"])
def test_unreadable_ollama_fails_closed(admission, failure):
    engine, reply, _ = admission
    if failure == "network":
        reply["error"] = TimeoutError("fixture")
    else:
        reply["body"] = b'{' if failure == "invalid_json" else b' ' * 256001
    assert engine.ready()[::2] == (False, "ollama_unavailable")


@pytest.mark.parametrize("table,values", [
    ("goal_model_calls", ("ubuntu_local", "started", "2099-01-01T00:00:00+00:00")),
    ("agent_jobs", ("code.build_project", "running", "2099-01-01T00:00:00+00:00")),
    ("agent_jobs", ("image.generate", "claimed", "2099-01-01T00:00:00+00:00")),
])
def test_active_swarmer_work_still_blocks_even_with_cpu_only_models(admission, table, values):
    engine, reply, probes = admission
    reply["body"] = b'{"models":[{"size_vram":0}]}'
    with sqlite3.connect(engine.settings.db) as db:
        db.execute(f"INSERT INTO {table} VALUES (?,?,?)", values)
    assert engine.ready()[::2] == (False, "swarmer_busy")
    assert probes == []  # Work already reserves the GPU; no further probe needed.


@pytest.mark.parametrize("condition,code", [
    ("executable", "runtime_unavailable"), ("model_absent", "model_unavailable"),
    ("model_changed", "model_invalid"), ("database", "probe_unavailable"),
])
def test_non_gpu_failures_have_distinct_reasons(admission, condition, code):
    engine, _, _ = admission
    if condition == "executable":
        (engine.settings.runtime / "sd-cli").chmod(0o600)
    elif condition == "model_absent":
        (engine.settings.models / "model.bin").unlink()
    elif condition == "model_changed":
        (engine.settings.models / "model.bin").write_bytes(b"changed")
    else:
        engine.settings.db.unlink()
    assert engine.ready()[::2] == (False, code)


@pytest.mark.parametrize("models,code,ready", [
    ([{"size_vram": 0}], "ready", True),
    ([{"size_vram": 1}], "ollama_gpu_reserved", False),
    ([{}], "ollama_unavailable", False),
])
def test_http_status_preserves_reason_and_blocks_unknown_before_queuing(admission, models, code, ready):
    engine, reply, _ = admission
    reply["body"] = json.dumps({"models": models}).encode()
    app = create_app(engine.settings, engine, allowed_hosts={"testserver"}, origins={"http://testserver"})
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["availability_code"] == code
        assert status["ready"] is ready
        assert status["jobs"] == [] and status["active_job"] is None
        if not ready:
            response = client.post("/api/jobs", json={"prompt": "teapot"},
                                   headers={"Origin": "http://testserver", "X-CSRF-Token": status["csrf_token"]})
            assert response.status_code == 409
            assert response.json()["detail"] == status["message"]
            assert app.state.manager.active is None and app.state.manager.jobs == {}
