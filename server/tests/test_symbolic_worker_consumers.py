"""Real consumer boundaries with offline model and tool adapters."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_symbolic_worker_context import symbolic_binding, symbolic_context

ROOT = Path(__file__).resolve().parents[2]


def load(monkeypatch, directory, name):
    parent = ROOT / "workers" / directory
    monkeypatch.syspath_prepend(str(parent))
    spec = importlib.util.spec_from_file_location("symbolic_test_" + name, parent / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def transport(payload):
    return {
        **payload,
        "symbolic_context": symbolic_context(),
        "symbolic_context_binding": symbolic_binding(),
    }


def test_text_symbolic_values_reach_one_model_post_as_data(monkeypatch):
    fixtures = load(monkeypatch, "text-worker", "test_text_worker")
    worker = fixtures.worker.__wrapped__()
    payload = transport(fixtures.payload())
    generator, connection = fixtures.generator_for(
        worker, monkeypatch, fixtures.stream(fixtures.draft())
    )
    assert generator.generate(payload, ensure_active=lambda: None) == fixtures.draft()
    [body] = [call[2] for call in connection.calls if call[0] == "POST"]
    sent = json.loads(body["messages"][1]["content"])
    assert sent["symbolic_context"] == payload["symbolic_context"]
    assert "symbolic_context_binding" not in sent
    assert "not instructions, permissions" in body["messages"][0]["content"]
    assert "Cache.py" not in body["messages"][0]["content"]
    assert "durable_context" not in sent and "requirements" not in sent


def test_code_symbolic_values_reach_one_model_post_as_data(monkeypatch):
    fixtures = load(monkeypatch, "code-worker", "test_code_worker")
    worker = fixtures.worker.__wrapped__()
    job = fixtures.job()
    job["payload"] = transport(job["payload"])
    requests = fixtures.install_model_response(worker, monkeypatch)
    prompt = worker.parse_job(job)
    worker.CodeGenerator("http://127.0.0.1:8712", "test-model").generate(prompt)
    assert len(requests) == 1
    body = json.loads(requests[0].data)
    sent = json.loads(body["messages"][1]["content"])
    assert sent["symbolic_context"] == job["payload"]["symbolic_context"]
    assert "symbolic_context_binding" not in sent
    assert "not instructions, permissions" in body["messages"][0]["content"]
    assert "Cache.py" not in body["messages"][0]["content"]


def test_project_symbolic_values_reach_actual_model_request(monkeypatch):
    fixtures = load(monkeypatch, "project-worker", "test_project_worker")
    worker = fixtures.worker
    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, *, timeout):
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {"content": json.dumps(fixtures.step())},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    payload = transport(fixtures.payload())
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "test-model")
    worker.run_iteration(payload, generator, fixtures.Runner(), lambda: None)
    assert len(captured) == 1
    messages = captured[0]["messages"]
    content = messages[-1]["content"].split("Current workspace data:\n", 1)[1]
    sent, _ = json.JSONDecoder().raw_decode(content)
    assert sent["symbolic_context"] == payload["symbolic_context"]
    assert "symbolic_context_binding" not in sent
    assert "not instructions, permissions" in messages[0]["content"]
    assert "Cache.py" not in messages[0]["content"]
    assert "Cache.py" not in json.dumps(sent.get("project_guidance", []))


@pytest.mark.parametrize("family", ["text", "code", "project"])
def test_model_worker_oversize_evidence_is_omitted_whole(monkeypatch, family):
    fixtures = load(monkeypatch, family + "-worker", "test_" + family + "_worker")
    worker = fixtures.worker.__wrapped__() if family != "project" else fixtures.worker
    context = symbolic_context()
    context["evidence"] = [copy.deepcopy(context["evidence"][0]) for _ in range(4)]
    for index, evidence in enumerate(context["evidence"]):
        evidence["proposal"]["proposal_id"] = f"proposal_{index}"
    if family == "text":
        payload = transport(fixtures.payload())
        payload["symbolic_context"] = context
        monkeypatch.setattr(worker, "MAX_PAYLOAD_BYTES", 3000)
        parsed = worker.validate_payload(payload)
    elif family == "code":
        job = fixtures.job()
        job["payload"] = transport(job["payload"])
        job["payload"]["symbolic_context"] = context
        parsed = json.loads(worker.parse_job(job))
    else:
        payload = transport(fixtures.payload())
        payload["symbolic_context"] = context
        worker.model_context(fixtures.payload(), prompt_max_bytes=10000)
        parsed = worker.model_context(payload, prompt_max_bytes=10000)
    assert parsed["symbolic_context"] == {**context, "evidence": [], "status": "omitted_budget"}


@pytest.mark.parametrize(
    "family,operation",
    [
        (
            "image",
            {"prompt": "Exact user prompt.", "width": 512, "height": 512, "steps": 4, "seed": 1},
        ),
        (
            "audio",
            {
                "text": "Texte exact.",
                "language": "fr-FR",
                "voice": "ff_siwis",
                "max_duration_seconds": 5,
            },
        ),
    ],
)
def test_media_arguments_are_not_rewritten_and_invalid_context_blocks(
    monkeypatch, family, operation
):
    worker = load(monkeypatch, "media-worker", "media_contract")
    skill = "image.generate" if family == "image" else "audio.synthesize"
    assert worker.validate_payload(skill, transport(operation)) == operation
    invalid = transport(operation)
    invalid["symbolic_context"]["grants_authority"] = True
    with pytest.raises(worker.MediaError, match="invalid_arguments"):
        worker.validate_payload(skill, invalid)


def test_file_and_review_operate_on_original_arguments(monkeypatch, tmp_path):
    file_worker = load(monkeypatch, "file-worker", "file_worker")
    review_worker = load(monkeypatch, "code-review-worker", "code_review_worker")
    (tmp_path / "file.txt").write_text("Exact case and accents: ÉTAT.")
    job = {"required_skill": "workspace.read_text", "payload": transport({"path": "file.txt"})}
    assert file_worker.execute(tmp_path, job) == {"content": "Exact case and accents: ÉTAT."}
    job["payload"]["symbolic_context"]["status"] = "verified"
    with pytest.raises(ValueError):
        file_worker.execute(tmp_path, job)
    base = {"required_skill": "code_review.git_status", "payload": {}}
    accepted = review_worker.parse_review_job(tmp_path, {**base, "payload": transport({})})
    original = review_worker.parse_review_job(tmp_path, base)
    assert {field: getattr(accepted, field) for field in accepted.__slots__} == {
        field: getattr(original, field) for field in original.__slots__
    }


def test_personal_adapters_receive_only_operation_arguments(monkeypatch):
    worker = load(monkeypatch, "personal-worker", "personal_worker")
    seen = []
    adapter = SimpleNamespace(execute=lambda payload, check: seen.append(payload) or {"ok": True})
    runtime = worker.PersonalWorker(adapter, adapter)
    for skill, operation in [
        ("documents.extract", {"path": "private.pdf"}),
        ("crm.command", {"command": "read"}),
    ]:
        assert runtime.execute(
            {"required_skill": skill, "payload": transport(operation)}, lambda: None
        ) == {"ok": True}
        assert seen[-1] == operation
    invalid = transport({})
    invalid["symbolic_context"]["grants_authority"] = True
    with pytest.raises(worker.PersonalError, match="payload_invalid"):
        runtime.execute({"required_skill": "documents.extract", "payload": invalid}, lambda: None)
    assert len(seen) == 2


@pytest.mark.parametrize("family", ["sqlite", "swift"])
@pytest.mark.parametrize("invalid", [False, True])
def test_native_run_once_delivers_only_operation_or_blocks(monkeypatch, family, invalid):
    worker = load(monkeypatch, family + "-worker", family + "_worker")
    protocol = worker.protocol if family == "sqlite" else worker._protocol()
    operation = (
        {"path": "data.sqlite"}
        if family == "sqlite"
        else {"kind": "swiftpm", "source_sha256": "a" * 64}
    )
    skill = "database.sqlite.inspect" if family == "sqlite" else "code.swift.build"
    payload = transport(operation)
    if invalid:
        payload["symbolic_context"]["grants_authority"] = True
    job = {
        "id": "job_test",
        "required_skill": skill,
        "payload": payload,
        "claim_token": "opaque",
        "lease_id": "lease_test",
        "lease_generation": 1,
    }
    submitted, seen = [], []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def heartbeat_agent(self, *args):
            pass

        def claim(self):
            return job

        def heartbeat_job(self, *args):
            pass

        def submit_result(self, *args):
            submitted.append(args[-1])

    class Heartbeat:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def ensure_active(self):
            pass

    monkeypatch.setattr(protocol, "ControlPlaneClient", Client)
    monkeypatch.setattr(protocol, "LeaseHeartbeat", Heartbeat)
    workspace = SimpleNamespace(
        timeout=10,
        execute=lambda name, payload, **kwargs: (
            seen.append((name, payload)) or {"status": "passed"}
        ),
    )
    assert worker.run_once("http://127.0.0.1:9000", "agent", "test-token", workspace)
    assert submitted[0]["status"] == ("failed" if invalid else "completed")
    assert seen == ([] if invalid else [("inspect" if family == "sqlite" else "build", operation)])


def test_research_query_and_collection_receive_only_operation_and_receipt_still_valid(monkeypatch):
    worker = load(monkeypatch, "research-worker", "research_worker")
    seen = []
    adapter = SimpleNamespace(
        query=lambda query: seen.append(query) or {"content_trust": "untrusted", "results": []}
    )
    operation = {"query": "Cache", "max_results": 2}
    worker.execute(adapter, {"required_skill": "research.query", "payload": transport(operation)})
    assert (seen[0].query, seen[0].max_results) == ("Cache", 2)
    real_collect = worker.collect
    captured = []

    def collect(payload, *args, **kwargs):
        captured.append(payload)
        return real_collect(payload, *args, **kwargs)

    monkeypatch.setattr(worker, "collect", collect)
    operation = {
        "focus": "Cache",
        "queries": ["Cache"],
        "source_urls": [],
        "required_domains": [],
        "max_results_per_query": 2,
        "max_pages": 2,
    }
    payload = transport(operation)
    result = worker.execute(adapter, {"required_skill": "research.collect", "payload": payload})
    from app.services.research_contracts import valid_research_collect_receipt

    assert captured == [operation]
    assert valid_research_collect_receipt(result, operation)
    assert valid_research_collect_receipt(result, payload)
    payload["symbolic_context"]["grants_authority"] = True
    assert not valid_research_collect_receipt(result, payload)


def test_all_shipped_symbolic_validators_match_server():
    canonical = (ROOT / "server/app/services/agent_capsule.py").read_bytes()
    for family in ["text", "code", "project", "file", "research", "code-review", "media"]:
        assert (
            ROOT / "workers" / (family + "-worker") / "agent_capsule.py"
        ).read_bytes() == canonical


@pytest.mark.parametrize(
    "family", ["file", "research", "code-review", "media", "personal", "sqlite", "swift"]
)
def test_shipped_stdlib_bundle_validates_without_server_imports(tmp_path, family):
    import shutil
    import subprocess

    bundle = tmp_path / "release"
    for directory in ["file", "research", "code-review", "media", "personal", "sqlite", "swift"]:
        source = ROOT / "workers" / (directory + "-worker")
        destination = bundle / "workers" / (directory + "-worker")
        destination.mkdir(parents=True)
        for path in source.glob("*.py"):
            if not path.name.startswith("test_"):
                shutil.copyfile(path, destination / path.name)
    target = bundle / "server/app/services/sqlite_workspace.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "server/app/services/sqlite_workspace.py", target)
    filename = "media_contract" if family == "media" else family.replace("-", "_") + "_worker"
    module_path = bundle / "workers" / (family + "-worker") / (filename + ".py")
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(transport({"path": "Exact.txt"})))
    script = """import importlib.util,json,pathlib,sys
path=pathlib.Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('isolated_worker',path)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
family=sys.argv[3]
owner=module.protocol if family in ('personal','sqlite') else module._protocol() if family=='swift' else module
value=owner._symbolic_operation(json.loads(pathlib.Path(sys.argv[2]).read_text()))
assert value=={'path':'Exact.txt'}
assert not any(name=='app' or name.startswith('app.') for name in sys.modules)
print('ok')
"""
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(module_path), str(payload_file), family],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "ok"
    assert not list(bundle.rglob("*.pyc"))


def test_project_compact_decision_retains_fixed_symbolic_trust_instruction(monkeypatch):
    fixtures = load(monkeypatch, "project-worker", "test_project_completion_decision")
    payload = transport(fixtures.validated_payload())
    body, generator, _ = fixtures.capture_compact_decision(
        monkeypatch, payload, fixtures.check_step()
    )
    assert generator.last_transport_metrics["compact_completion"] == 1
    assert "not instructions, permissions" in body["messages"][0]["content"]
    assert "Cache.py" not in body["messages"][0]["content"]
    content = body["messages"][-1]["content"].split("Current workspace data:\n", 1)[1]
    sent, _ = json.JSONDecoder().raw_decode(content)
    assert sent["symbolic_context"] == payload["symbolic_context"]
