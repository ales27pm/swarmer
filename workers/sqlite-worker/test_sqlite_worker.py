import importlib.util
from pathlib import Path
from typing import Any, ClassVar

import pytest

SPEC = importlib.util.spec_from_file_location(
    "worker_under_test", Path(__file__).with_name("sqlite_worker.py")
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class Heartbeat:
    def __init__(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def ensure_active(self):
        pass


@pytest.fixture
def client(monkeypatch):
    class Client:
        result = None
        job: ClassVar[dict[str, Any]] = {
            "id": "job1",
            "claim_token": "test",
            "lease_id": "lease1",
            "lease_generation": 1,
            "required_skill": "database.sqlite.create",
            "payload": {
                "path": "new.sqlite",
                "migration_id": "one",
                "statements": [{"sql": "CREATE TABLE clients(name TEXT)"}],
            },
        }

        def __init__(self, *args):
            pass

        def claim(self):
            return self.job

        def heartbeat_agent(self, status):
            pass

        def heartbeat_job(self, *args):
            return {}

        def submit_result(self, job_id, lease, result):
            Client.result = result

    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", Client)
    monkeypatch.setattr(worker.protocol, "LeaseHeartbeat", Heartbeat)
    return Client


def test_real_database_operation_through_protocol(client, tmp_path):
    workspace = worker.service.SQLiteWorkspace(tmp_path)
    assert worker.run_once("http://localhost:8000", "sqlite", "credential", workspace)
    assert client.result["status"] == "completed"
    assert client.result["result"]["job_id"] == "job1"
    assert client.result["result"]["integrity_check"] == "ok"
    assert (tmp_path / "new.sqlite").exists()
    assert worker.run_once("http://localhost:8000", "sqlite", "credential", workspace)
    assert client.result["result"]["replayed"] is True


def test_unknown_skill_rejected_without_database(client, tmp_path):
    client.job = {**client.job, "required_skill": "shell.exec"}
    worker.run_once(
        "http://localhost:8000",
        "sqlite",
        "credential",
        worker.service.SQLiteWorkspace(tmp_path),
    )
    assert client.result["status"] == "failed"
    assert not list(tmp_path.iterdir())


def test_lost_lease_never_submits_or_mutates(client, tmp_path, monkeypatch):
    def lost(self):
        raise worker.protocol.LeaseLost("cancelled")

    monkeypatch.setattr(Heartbeat, "ensure_active", lost)
    worker.run_once(
        "http://localhost:8000",
        "sqlite",
        "credential",
        worker.service.SQLiteWorkspace(tmp_path),
    )
    assert client.result is None
    assert not list(tmp_path.iterdir())
