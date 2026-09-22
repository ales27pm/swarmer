from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "personal_worker_test", Path(__file__).with_name("personal_worker.py")
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def test_local_text_real_bytes_and_bounds(tmp_path):
    content = "Exigence: garder les accents français.\n" * 20
    (tmp_path / "notes.txt").write_text(content, encoding="utf8")
    extractor = worker.DocumentExtractor(tmp_path)
    result = extractor.execute({"path": "notes.txt", "max_characters": 40}, lambda: None)
    assert result["text"] == content[:40]
    assert result["partial"] is True
    assert result["source"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert result["pages"] == []
    assert result["content_trust"] == "untrusted"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/tmp/file.txt",
        ".env",
        "api-token.txt",
        "https://example.com/x.pdf",
    ],
)
def test_document_paths_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        worker.DocumentExtractor(tmp_path).execute({"path": path}, lambda: None)


def test_document_symlinks_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("private")
    (tmp_path / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError):
        worker.DocumentExtractor(tmp_path).execute({"path": "link.txt"}, lambda: None)


def test_optional_docling_reports_missing_dependency_without_false_success(tmp_path):
    if importlib.util.find_spec("docling"):
        pytest.skip("dependency installed: exercise actual fixture in qualified profile")
    (tmp_path / "sample.pdf").write_bytes(b"%PDF-1.4\n")
    with pytest.raises(worker.PersonalError, match="docling_unavailable"):
        worker.DocumentExtractor(tmp_path).execute({"path": "sample.pdf"}, lambda: None)


def test_lease_loss_stops_extraction(tmp_path):
    (tmp_path / "sample.txt").write_text("test")

    def cancelled():
        raise worker.protocol.LeaseLost("test lease")

    with pytest.raises(worker.protocol.LeaseLost):
        worker.DocumentExtractor(tmp_path).execute({"path": "sample.txt"}, cancelled)


@pytest.mark.parametrize(
    "origin,allowed",
    [
        ("http://crm.example", ("http://crm.example",)),
        ("https://crm.example/path", ("https://crm.example/path",)),
        ("https://user:pass@crm.example", ("https://user:pass@crm.example",)),
        ("https://crm.example", ("https://other.example",)),
    ],
)
def test_crm_origins_fail_closed(origin, allowed):
    with pytest.raises(worker.PersonalError):
        worker.CrmAdapter(origin, "x" * 32, allowed)


class Response:
    def __init__(self, response, connection):
        self.response = response
        self.connection = connection
        self.status = response.status
        self.headers = response.headers

    def read(self, limit):
        return self.response.read(limit)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()


def test_crm_http_contract_stable_key_no_token_or_endpoint_in_body():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, dict(self.headers), payload))
            result = {
                "operationId": "receipt-1",
                "resource": "tasks",
                "persisted": True,
                "item": {"id": "task-1", "title": "Test"},
            }
            data = json.dumps(result).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalTransport:
        def open(self, request, timeout):
            assert request.full_url == "https://crm.example/api/integrations/v1/commands"
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=timeout
            )
            connection.request(
                request.method,
                "/api/integrations/v1/commands",
                body=request.data,
                headers=dict(request.header_items()),
            )
            return Response(connection.getresponse(), connection)

    try:
        adapter = worker.CrmAdapter(
            "https://crm.example",
            "secret-server-token-with-32-chars",
            ("https://crm.example",),
            opener=LocalTransport(),
        )
        command = {
            "operation": "tasks.create",
            "data": {"title": "Test", "dealId": "deal-1"},
            "idempotency_key": "approved-task-123",
        }
        for _ in range(2):
            result = adapter.execute(command, lambda: None)
            assert result["receipt"]["operationId"] == "receipt-1"
        for path, headers, payload in calls:
            assert path == "/api/integrations/v1/commands"
            lower = {k.lower(): v for k, v in headers.items()}
            assert lower["authorization"] == "Bearer secret-server-token-with-32-chars"
            assert lower["idempotency-key"] == "approved-task-123"
            assert payload == {
                "operation": "tasks.create",
                "data": {"title": "Test", "dealId": "deal-1"},
            }
        for bad in [
            {**command, "endpoint": "https://evil.example"},
            {**command, "idempotency_key": None},
            {"operation": "messages.send", "data": {}},
        ]:
            with pytest.raises(worker.PersonalError):
                adapter.execute(bad, lambda: None)
        assert len(calls) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_crm_redirects_are_not_followed():
    redirect = worker.protocol._RejectRedirects()
    assert (
        redirect.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example")
        is None
    )


def test_lease_fenced_worker_result_and_stale_suppression(tmp_path, monkeypatch):
    (tmp_path / "test.txt").write_text("Real document")
    calls = []
    stale = [False]

    class Client:
        def __init__(self, *args):
            pass

        def heartbeat_agent(self, status):
            calls.append(("agent", status))

        def claim(self):
            return {
                "id": "job-1",
                "required_skill": "documents.extract",
                "payload": {"path": "test.txt"},
                "claim_token": "opaque",
                "lease_id": "lease-1",
                "lease_generation": 2,
            }

        def heartbeat_job(self, job_id, lease):
            if stale[0]:
                raise worker.protocol.LeaseLost("stale")
            return {}

        def submit_result(self, job_id, lease, result):
            calls.append(("result", result, lease.lease_generation))

    monkeypatch.setattr(worker.protocol, "ControlPlaneClient", Client)
    instance = worker.PersonalWorker(worker.DocumentExtractor(tmp_path))
    assert worker.run_once("http://localhost:8000", "agent", "credential", instance)
    results = [c for c in calls if c[0] == "result"]
    assert len(results) == 1
    assert results[0][1]["result"]["text"] == "Real document"
    assert results[0][2] == 2
    stale[0] = True
    calls.clear()
    assert worker.run_once("http://localhost:8000", "agent", "credential", instance)
    assert not any(c[0] == "result" for c in calls)
