import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
from runner import DockerRunner, Settings, acquire_gpu_lock, parse_progress, read_png
from studio import JobManager, create_app


class FakeRunner:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.busy = False
        self.cleanups = 0

    def cleanup_interrupted(self):
        self.cleanups += 1

    def ready(self):
        return (False, "GPU occupé", "ollama_gpu_reserved") if self.busy else (True, "Prêt", "ready")

    def run(self, job, work, cancel, update):
        self.started.set()
        update(status="running", phase="encoding", progress=None)
        while not self.release.wait(0.01):
            if cancel.is_set():
                return {"status": "cancelled", "phase": "cancelled", "error": None}
        update(phase="sampling", progress=0.5)
        Image.new("RGB", (512, 512), (10, 20, 30)).save(work / "image.png")
        return {"status": "completed", "phase": "completed", "error": None}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(tmp_path / "state", tmp_path / "models", tmp_path / "runtime", tmp_path / "state.sqlite", gpu_lock=tmp_path / "gpu/image-generation.lock")
    runner = FakeRunner()
    app = create_app(settings, runner, allowed_hosts={"testserver"}, origins={"http://testserver"})
    with TestClient(app) as client:
        token = client.get("/api/status").json()["csrf_token"]
        headers = {"X-CSRF-Token": token, "Origin": "http://testserver"}
        yield client, runner, headers, settings, app


def wait_status(client, identifier, target):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = client.get("/api/jobs/" + identifier).json()
        if value["status"] == target:
            return value
        time.sleep(0.01)
    pytest.fail(f"did not reach {target}: {value}")


def test_real_lifecycle_persist_image_and_single_active(setup):
    client, runner, headers, settings, _ = setup
    assert client.get("/api/status").json()["jobs"] == []
    created = client.post("/api/jobs", json={"prompt": "A red teapot", "seed": None}, headers=headers)
    assert created.status_code == 202
    identifier = created.json()["id"]
    assert 0 <= created.json()["seed"] <= 2147483647
    assert runner.started.wait(1)
    assert settings.gpu_lock.read_bytes() == expected_marker(settings)
    assert client.post("/api/jobs", json={"prompt": "another"}, headers=headers).status_code == 409
    assert client.get(f"/api/jobs/{identifier}/image").status_code == 409
    status = client.get("/api/status").json()
    assert not status["ready"]
    assert status["availability_code"] == "studio_active"
    runner.release.set()
    finished = wait_status(client, identifier, "completed")
    assert finished["progress"] == 1
    assert finished["elapsed_seconds"] > 0
    image = client.get(finished["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    assert "attachment" in client.get(finished["image_url"] + "?download=1").headers["content-disposition"]
    stored = json.loads((settings.state / identifier / "job.json").read_text())
    assert stored["status"] == "completed"
    assert (settings.state.stat().st_mode & 0o777) == 0o700
    assert client.get("/api/status").json()["ready"]
    assert settings.gpu_lock.read_bytes() == b""


def test_cancellation_is_idempotent_and_keeps_gpu_lock_until_stopped(setup):
    client, runner, headers, settings, _ = setup
    identifier = client.post("/api/jobs", json={"prompt": "A tree"}, headers=headers).json()["id"]
    assert runner.started.wait(1)
    response = client.post(f"/api/jobs/{identifier}/cancel", json={}, headers=headers)
    assert response.status_code == 200
    finished = wait_status(client, identifier, "cancelled")
    assert finished["image_url"] is None
    assert client.post(f"/api/jobs/{identifier}/cancel", json={}, headers=headers).json()["status"] == "cancelled"
    assert client.get("/api/status").json()["active_job"] is None
    assert settings.gpu_lock.read_bytes() == b""


@pytest.mark.parametrize("body", [
    {"prompt": " "}, {"prompt": "x" * 2001}, {"prompt": "nul\x00"}, {"prompt": 4},
    {"prompt": "x", "steps": True}, {"prompt": "x", "steps": "40"},
    {"prompt": "x", "steps": 41}, {"prompt": "x", "steps": 0},
    {"prompt": "x", "seed": False}, {"prompt": "x", "seed": -1},
    {"prompt": "x", "width": 768}, {"prompt": "x", "height": 511},
    {"prompt": "x", "output": "/tmp/pwn"}, {"prompt": "x", "negative_prompt": "x" * 2001},
])
def test_input_contract_strict(setup, body):
    client, runner, headers, *_ = setup
    assert client.post("/api/jobs", json=body, headers=headers).status_code == 422
    assert not runner.started.is_set()


def test_csrf_origin_host_and_content_type(setup):
    client, _, headers, *_ = setup
    assert client.post("/api/jobs", json={"prompt": "x"}).status_code == 403
    assert client.post("/api/jobs", json={"prompt": "x"}, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/jobs", json={"prompt": "x"}, headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.test"}).status_code == 403
    assert client.post("/api/jobs", content="prompt=x", headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/api/jobs", content="x" * 16385, headers={**headers, "Content-Type": "application/json"}).status_code == 413
    response = client.get("/api/status")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_busy_production_rejected_without_work(setup):
    client, runner, headers, *_ = setup
    runner.busy = True
    assert not client.get("/api/status").json()["ready"]
    assert client.post("/api/jobs", json={"prompt": "x"}, headers=headers).status_code == 409
    assert not runner.started.is_set()


def test_restart_marks_interrupted_failed_and_retains_completed(tmp_path):
    settings = Settings(tmp_path, tmp_path / "models", tmp_path / "runtime", tmp_path / "db", gpu_lock=tmp_path / "gpu.lock")
    for identifier, status in [("a" * 32, "running"), ("b" * 32, "completed")]:
        work = tmp_path / identifier
        work.mkdir()
        (work / "job.json").write_text(json.dumps({"id": identifier, "status": status, "phase": "sampling", "progress": 0.5}))
    runner = FakeRunner()
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert runner.cleanups == 1
        assert manager.jobs["a" * 32]["status"] == "failed"
        assert "redémarré" in manager.jobs["a" * 32]["error"]
        assert manager.jobs["b" * 32]["status"] == "completed"
        second = JobManager(settings, FakeRunner())
        with pytest.raises(BlockingIOError):
            second.start()
    finally:
        manager.close()


def test_command_keeps_prompt_one_argument_and_runtime_isolation(tmp_path):
    settings = Settings(tmp_path, tmp_path / "models", tmp_path / "runtime", tmp_path / "db")
    runner = DockerRunner(settings)
    prompt = 'one teapot; $(touch /tmp/pwn) " -o /etc/passwd'
    args = runner.build_command({"id": "a" * 32, "prompt": prompt, "negative_prompt": "", "steps": 40, "seed": 42}, tmp_path, "test")
    assert args[args.index("-p") + 1] == prompt
    assert args[args.index("--params-backend") + 1] == "diffusion=cuda0,te=cpu,vae=cpu"
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--memory") + 1] == "12g"
    assert args[args.index("--entrypoint") + 1] == "/usr/bin/timeout"
    assert "900" in args
    assert "--read-only" in args
    assert args[args.index("-o") + 1] == "/output/image.png"
    assert "sh" not in args and "bash" not in args


def test_progress_comes_only_from_sampling_counter():
    assert parse_progress(" |==========> | 10/40 - 4.1s/it\x1b[K", 40) == ("sampling", 0.25)
    assert parse_progress(" |########## | 40/643 - 4.2GB/s", 40) is None
    assert parse_progress('[VERBOSE] prompt "40/40 - done"', 40) is None
    assert parse_progress("[INFO] image.cpp:866 - generating image: 1/1 - seed 42", 1) == ("sampling", None)
    assert parse_progress("generating image: 1/1 - seed 42", 1) is None
    assert parse_progress("[INFO] sampling completed, taking 164.86s", 40) == ("decoding", None)


def test_png_rejects_truncation_and_wrong_dimensions(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (512, 512)).save(image)
    assert read_png(image, 512, 512)["bytes"] > 24
    with pytest.raises(RuntimeError, match="unexpected_image_dimensions"):
        read_png(image, 768, 768)
    image.write_bytes(image.read_bytes()[:24])
    with pytest.raises(RuntimeError, match="invalid_image_output"):
        read_png(image, 512, 512)


def test_cleanup_failure_blocks_subsequent_generation(setup):
    client, runner, headers, settings, _ = setup
    def fail_cleanup(*args):
        raise RuntimeError("renderer_cleanup_failed")
    runner.run = fail_cleanup
    identifier = client.post("/api/jobs", json={"prompt": "x"}, headers=headers).json()["id"]
    failed = wait_status(client, identifier, "failed")
    assert "nettoyage" in failed["error"]
    status = client.get("/api/status").json()
    assert status["active_job"] is None
    assert not status["ready"]
    assert status["availability_code"] == "cleanup_required"
    assert "nettoyage" in status["message"]
    assert client.post("/api/jobs", json={"prompt": "again"}, headers=headers).status_code == 503
    with pytest.raises(BlockingIOError):
        acquire_gpu_lock(setup[3].gpu_lock)
    assert settings.gpu_lock.read_bytes() == expected_marker(settings)


def test_worker_shared_lock_rejects_before_job_is_queued(setup):
    client, runner, headers, settings, _ = setup
    worker_lock = acquire_gpu_lock(settings.gpu_lock)
    try:
        status = client.get("/api/status").json()
        assert not status["ready"]
        assert status["availability_code"] == "image_slot_reserved"
        assert status["message"] == "Le GPU est réservé à une autre tâche locale."
        response = client.post("/api/jobs", json={"prompt": "x"}, headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"] == status["message"]
        assert client.get("/api/status").json()["jobs"] == []
        assert not runner.started.is_set()
    finally:
        os.close(worker_lock)
    assert client.get("/api/status").json()["ready"]


def test_studio_holds_shared_lock_against_other_process_through_cleanup(setup):
    client, runner, headers, settings, _ = setup
    cleanup_started, cleanup_done = threading.Event(), threading.Event()
    def delayed_cleanup(job, work, cancel, update):
        update(status="running", phase="sampling", progress=0.5)
        runner.started.set()
        cancel.wait(2)
        cleanup_started.set()
        cleanup_done.wait(2)
        return {"status": "cancelled", "phase": "cancelled", "error": None}
    runner.run = delayed_cleanup
    identifier = client.post("/api/jobs", json={"prompt": "x"}, headers=headers).json()["id"]
    assert runner.started.wait(1)
    probe = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)", str(settings.gpu_lock)]
    assert subprocess.run(probe, capture_output=True, check=False, timeout=5).returncode != 0
    client.post(f"/api/jobs/{identifier}/cancel", json={}, headers=headers)
    assert cleanup_started.wait(1)
    assert subprocess.run(probe, capture_output=True, check=False, timeout=5).returncode != 0
    cleanup_done.set()
    wait_status(client, identifier, "cancelled")
    assert subprocess.run(probe, capture_output=True, check=False, timeout=5).returncode == 0


def test_inaccessible_shared_lock_fails_closed(setup):
    client, runner, headers, settings, _ = setup
    # A directory cannot be used as a lock file; simulate a deployment mistake.
    settings.gpu_lock.unlink()
    settings.gpu_lock.mkdir()
    status = client.get("/api/status").json()
    assert not status["ready"]
    assert status["availability_code"] == "gpu_lock_unavailable"
    response = client.post("/api/jobs", json={"prompt": "x"}, headers=headers)
    assert response.status_code == 503
    assert "verrou" in response.json()["detail"]
    assert not runner.started.is_set()


def test_queued_persistence_failure_releases_shared_lock(setup, monkeypatch):
    _, _, _, settings, app = setup
    from studio import JobRequest
    manager = app.state.manager
    def broken_save(job):
        raise OSError("no space")
    monkeypatch.setattr(manager, "_save", broken_save)
    with pytest.raises(OSError):
        manager.create(JobRequest(prompt="x"))
    assert manager.active is None
    descriptor = acquire_gpu_lock(settings.gpu_lock)
    os.close(descriptor)
    assert settings.gpu_lock.read_bytes() == b""


def test_shared_lock_default_and_operator_override(monkeypatch, tmp_path):
    monkeypatch.delenv("CHROMA_STUDIO_GPU_LOCK", raising=False)
    assert Settings.from_env().gpu_lock == Path.home() / ".local/state/swarmer-gpu/image-generation.lock"
    monkeypatch.setenv("CHROMA_STUDIO_GPU_LOCK", str(tmp_path / "shared.lock"))
    assert Settings.from_env().gpu_lock == tmp_path / "shared.lock"


def expected_marker(settings):
    scope = hashlib.sha256(str(settings.state.resolve()).encode()).hexdigest()[:16]
    return f"chroma-studio-v1:{scope}\n".encode()


def isolated_settings(tmp_path):
    return Settings(tmp_path / "state", tmp_path / "models", tmp_path / "runtime", tmp_path / "db",
                    gpu_lock=tmp_path / "gpu.lock")


def test_readiness_is_rechecked_under_lock_before_accepting_job(setup, monkeypatch):
    client, runner, headers, settings, _ = setup
    def backend_commits_before_studio_acquires(path):
        descriptor = acquire_gpu_lock(path)
        runner.busy = True
        return descriptor
    monkeypatch.setattr(studio, "acquire_gpu_lock", backend_commits_before_studio_acquires)
    response = client.post("/api/jobs", json={"prompt": "x"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "GPU occupé"
    assert not runner.started.is_set()
    assert not list(settings.state.glob("*/job.json"))
    assert settings.gpu_lock.read_bytes() == b""
    descriptor = acquire_gpu_lock(settings.gpu_lock)
    os.close(descriptor)


def test_reservation_is_synced_before_thread_starts(setup, monkeypatch):
    client, runner, headers, settings, _ = setup
    real_fsync, real_run = os.fsync, runner.run
    synced = threading.Event()
    def observe_fsync(descriptor):
        real_fsync(descriptor)
        if os.fstat(descriptor).st_ino == settings.gpu_lock.stat().st_ino:
            assert settings.gpu_lock.read_bytes() == expected_marker(settings)
            synced.set()
    def assert_reserved(*args):
        assert synced.is_set()
        assert settings.gpu_lock.read_bytes() == expected_marker(settings)
        return real_run(*args)
    monkeypatch.setattr(studio.os, "fsync", observe_fsync)
    monkeypatch.setattr(runner, "run", assert_reserved)
    client.post("/api/jobs", json={"prompt": "x"}, headers=headers)
    assert runner.started.wait(1)
    # Restore before the cancellation/terminal cleanup deliberately syncs empty.
    monkeypatch.setattr(studio.os, "fsync", real_fsync)


@pytest.mark.parametrize("marker", [b"another-worker-v1\n", b"chroma-studio-v1:foreign\n", b"\x00"])
def test_foreign_marker_blocks_start_and_create_without_cleanup(tmp_path, marker):
    settings = isolated_settings(tmp_path)
    settings.gpu_lock.write_bytes(marker)
    inode = settings.gpu_lock.stat().st_ino
    runner = FakeRunner()
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert runner.cleanups == 0
        assert manager.status()["availability_code"] == "cleanup_required"
        with pytest.raises(studio.HTTPException) as error:
            manager.create(studio.JobRequest(prompt="x"))
        assert error.value.status_code == 409
        assert not runner.started.is_set()
        assert runner.cleanups == 0
        assert settings.gpu_lock.read_bytes() == marker
        assert settings.gpu_lock.stat().st_ino == inode
    finally:
        manager.close()
    assert settings.gpu_lock.read_bytes() == marker


def test_startup_with_worker_lock_defers_recovery_until_create(tmp_path):
    settings = isolated_settings(tmp_path)
    worker = acquire_gpu_lock(settings.gpu_lock)
    runner = FakeRunner()
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert runner.cleanups == 0
        assert manager.status()["availability_code"] == "image_slot_reserved"
        os.close(worker)
        worker = None
        assert manager.status()["ready"]
        manager.create(studio.JobRequest(prompt="x"))
        assert runner.started.wait(1)
        assert runner.cleanups == 1
        assert settings.gpu_lock.read_bytes() == expected_marker(settings)
    finally:
        if worker is not None:
            os.close(worker)
        manager.close()


def test_readiness_recovers_own_marker_after_transient_startup_lock(tmp_path):
    settings = isolated_settings(tmp_path)
    settings.gpu_lock.write_bytes(expected_marker(settings))
    inode = settings.gpu_lock.stat().st_ino
    worker = acquire_gpu_lock(settings.gpu_lock)
    runner = FakeRunner()
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert manager.status()["availability_code"] == "image_slot_reserved"
        assert runner.cleanups == 0
        os.close(worker)
        worker = None
        status = manager.status()
        assert status["ready"]
        assert status["availability_code"] == "ready"
        assert runner.cleanups == 1
        assert not manager.recovery_pending
        assert settings.gpu_lock.read_bytes() == b""
        assert settings.gpu_lock.stat().st_ino == inode
        assert manager.jobs == {}
        assert not runner.started.is_set()
        assert manager.status()["ready"]
        assert runner.cleanups == 1
    finally:
        if worker is not None:
            os.close(worker)
        manager.close()


def test_readiness_cleanup_failure_is_closed_and_can_recover_later(tmp_path):
    settings = isolated_settings(tmp_path)
    settings.gpu_lock.write_bytes(expected_marker(settings))
    worker = acquire_gpu_lock(settings.gpu_lock)
    runner = FakeRunner()
    manager = JobManager(settings, runner)
    manager.start()
    os.close(worker)
    original = runner.cleanup_interrupted
    def fail_cleanup():
        raise RuntimeError("Docker cleanup is unavailable")
    runner.cleanup_interrupted = fail_cleanup
    try:
        status = manager.status()
        assert not status["ready"]
        assert status["availability_code"] == "cleanup_required"
        assert manager.recovery_pending
        assert settings.gpu_lock.read_bytes() == expected_marker(settings)
        assert not runner.started.is_set()
        # The failed probe releases flock but preserves the durable reservation.
        descriptor = acquire_gpu_lock(settings.gpu_lock)
        os.close(descriptor)
        runner.cleanup_interrupted = original
        assert manager.status()["ready"]
        assert runner.cleanups == 1
        assert settings.gpu_lock.read_bytes() == b""
        assert manager.jobs == {}
    finally:
        manager.close()


def test_restart_recovers_only_own_marker_after_cleanup(tmp_path):
    settings = isolated_settings(tmp_path)
    settings.gpu_lock.write_bytes(expected_marker(settings))
    inode = settings.gpu_lock.stat().st_ino
    runner = FakeRunner()
    original = runner.cleanup_interrupted
    def cleanup():
        assert settings.gpu_lock.read_bytes() == expected_marker(settings)
        with pytest.raises(BlockingIOError):
            acquire_gpu_lock(settings.gpu_lock)
        original()
    runner.cleanup_interrupted = cleanup
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert runner.cleanups == 1
        assert settings.gpu_lock.read_bytes() == b""
        assert settings.gpu_lock.stat().st_ino == inode
    finally:
        manager.close()


def test_failed_startup_cleanup_retains_durable_marker(tmp_path):
    settings = isolated_settings(tmp_path)
    settings.gpu_lock.write_bytes(expected_marker(settings))
    runner = FakeRunner()
    def fail_cleanup():
        raise RuntimeError("docker unavailable")
    runner.cleanup_interrupted = fail_cleanup
    manager = JobManager(settings, runner)
    with pytest.raises(RuntimeError, match="docker unavailable"):
        manager.start()
    assert settings.gpu_lock.read_bytes() == expected_marker(settings)
    assert manager.state_lock is None


def test_crash_releases_flock_but_not_durable_reservation(tmp_path):
    settings = isolated_settings(tmp_path)
    script = """
import os, sys, threading, time
from pathlib import Path
from runner import Settings
from studio import JobManager, JobRequest
root = Path(sys.argv[1])
class Runner:
    def cleanup_interrupted(self): pass
    def ready(self): return True, '', 'ready'
    def run(self, *args):
        (root / 'renderer-survives').write_text('running')
        os._exit(23)
manager = JobManager(Settings(root/'state', root/'models', root/'runtime', root/'db', gpu_lock=root/'gpu.lock'), Runner())
manager.start()
manager.create(JobRequest(prompt='crash fixture'))
time.sleep(5)
os._exit(24)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=Path(studio.__file__).parent,
                            timeout=10, capture_output=True, check=False)
    assert result.returncode == 23, result.stderr.decode()
    inode = settings.gpu_lock.stat().st_ino
    descriptor = acquire_gpu_lock(settings.gpu_lock)
    try:
        assert os.read(descriptor, 128) == expected_marker(settings)
    finally:
        os.close(descriptor)
    # Recovery represents positively stopping the renderer that survived the
    # service process. The reservation must exist throughout that cleanup.
    runner = FakeRunner()
    def cleanup():
        assert settings.gpu_lock.read_bytes() == expected_marker(settings)
        (tmp_path / "renderer-survives").unlink()
    runner.cleanup_interrupted = cleanup
    manager = JobManager(settings, runner)
    manager.start()
    try:
        assert not (tmp_path / "renderer-survives").exists()
        assert settings.gpu_lock.read_bytes() == b""
        assert settings.gpu_lock.stat().st_ino == inode
        assert all(job["status"] == "failed" for job in manager.jobs.values())
    finally:
        manager.close()


def test_run_error_with_unconfirmed_cleanup_keeps_marker(setup, monkeypatch):
    client, runner, headers, settings, _ = setup
    def broken_run(*args):
        raise OSError("receipt write failed after uncertain cleanup")
    def broken_cleanup():
        raise RuntimeError("cleanup unavailable")
    monkeypatch.setattr(runner, "run", broken_run)
    monkeypatch.setattr(runner, "cleanup_interrupted", broken_cleanup)
    identifier = client.post("/api/jobs", json={"prompt": "x"}, headers=headers).json()["id"]
    wait_status(client, identifier, "failed")
    assert settings.gpu_lock.read_bytes() == expected_marker(settings)
    assert client.get("/api/status").json()["availability_code"] == "cleanup_required"
    # Permit fixture teardown to demonstrate a later positive cleanup can clear.
    monkeypatch.setattr(runner, "cleanup_interrupted", lambda: None)
