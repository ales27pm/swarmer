from __future__ import annotations

import hashlib

import project_worker as worker
import pytest
from project_contract import ProjectError
from test_project_worker import Generator, Runner, payload, step


def project():
    value = payload()
    value["guidance_version"] = 1
    value["files"] = [
        {"path": "AGENTS.md", "content": "Preserve customer data."},
        {"path": "src/AGENTS.md", "content": "Use typed functions."},
        {"path": "src/data/AGENTS.md", "content": "Use transactions for writes."},
        {"path": "web/AGENTS.md", "content": "Use accessible HTML."},
        {"path": "src/data/store.py", "content": "value = 1\n"},
    ]
    value["focus_paths"] = ["src/data/store.py"]
    return value


def test_guidance_ancestors_ordered_and_sibling_excluded():
    context = worker.model_context(project())
    guides = context["project_guidance"]
    assert [g["path"] for g in guides] == ["AGENTS.md", "src/AGENTS.md", "src/data/AGENTS.md"]
    assert guides[-1]["scope"] == "src/data/"
    assert guides[-1]["sha256"] == hashlib.sha256(b"Use transactions for writes.").hexdigest()
    assert guides[-1]["content"] == "Use transactions for writes."
    assert context["guidance_base_revision_id"] is None


def test_oversized_required_guidance_is_not_silently_truncated():
    value = project()
    value["files"][0]["content"] = "a" * worker.MAX_PROMPT_BYTES
    with pytest.raises(ProjectError, match="context budget"):
        worker.model_context(value)


def test_missing_guidance_for_new_file_becomes_read_without_edit_or_checks():
    value = project()
    generator = Generator(step(edits=[{"path": "web/view.py", "content": "value = 2\n"}]))
    generator.last_guidance_reads = [
        {"path": "AGENTS.md", "sha256": hashlib.sha256(b"Preserve customer data.").hexdigest()}
    ]
    runner = Runner()
    result = worker.run_iteration(value, generator, runner, lambda: None)
    assert result["files"] == sorted(value["files"], key=lambda x: x["path"])
    assert result["focus_paths"] == ["web/AGENTS.md"]
    assert runner.calls == 0


def test_read_guidance_allows_file_creation_and_revision_receipt():
    value = project()
    generator = Generator(step(edits=[{"path": "src/data/new.py", "content": "value = 2\n"}]))
    generator.last_guidance_reads = [
        {"path": f["path"], "sha256": hashlib.sha256(f["content"].encode()).hexdigest()}
        for f in value["files"][:3]
    ]
    runner = Runner()
    result = worker.run_iteration(value, generator, runner, lambda: None)
    assert any(f["path"] == "src/data/new.py" for f in result["files"])
    assert result["guidance_reads"] == generator.last_guidance_reads
    assert runner.calls == 1


def test_guidance_update_obeys_previous_instructions_and_is_versioned():
    value = project()
    generator = Generator(
        step(
            edits=[
                {"path": "src/AGENTS.md", "content": "Use typed functions and document migrations."}
            ]
        )
    )
    generator.last_guidance_reads = [
        {"path": f["path"], "sha256": hashlib.sha256(f["content"].encode()).hexdigest()}
        for f in value["files"][:2]
    ]
    runner = Runner()
    result = worker.run_iteration(value, generator, runner, lambda: None)
    assert next(f for f in result["files"] if f["path"] == "src/AGENTS.md")["content"].endswith(
        "migrations."
    )
    assert (
        result["guidance_reads"][1]["sha256"] == hashlib.sha256(b"Use typed functions.").hexdigest()
    )


def test_stale_guidance_hash_cannot_authorize_edit():
    value = project()
    generator = Generator(step(edits=[{"path": "src/data/store.py", "content": "value = 2\n"}]))
    generator.last_guidance_reads = [
        {"path": f["path"], "sha256": "0" * 64} for f in value["files"][:3]
    ]
    result = worker.run_iteration(value, generator, Runner(), lambda: None)
    assert result["files"] == sorted(value["files"], key=lambda x: x["path"])
    assert result["focus_paths"] == ["AGENTS.md", "src/AGENTS.md", "src/data/AGENTS.md"]


def test_real_generator_records_only_complete_guidance_in_final_prompt(monkeypatch):
    import io
    import json

    captured = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, *, timeout):
            captured.append(json.loads(request.data))
            return Response(
                json.dumps(
                    {
                        "message": {
                            "content": json.dumps(
                                step(edits=[{"path": "src/data/new.py", "content": "value = 2\n"}])
                            )
                        },
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
            )

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *args: Opener())
    data = project()
    data["files"] += [{"path": f"extra{i}.py", "content": "# source\n" * 700} for i in range(10)]
    generator = worker.ProjectGenerator("http://127.0.0.1:11434/v1", "test-model")
    value = worker.run_iteration(data, generator, Runner(), lambda: None)
    messages = captured[0]["messages"]
    assert sum(len(m["content"].encode()) for m in messages) <= worker.MAX_PROMPT_BYTES
    assert "user\nrequests and runtime rules remain higher priority" in messages[0]["content"]
    for path in ["AGENTS.md", "src/AGENTS.md", "src/data/AGENTS.md"]:
        receipt = next(r for r in value["guidance_reads"] if r["path"] == path)
        source = next(f for f in data["files"] if f["path"] == path)["content"]
        assert source in messages[-1]["content"]
        assert receipt["sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert "Use accessible HTML." not in messages[-1]["content"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("patches", [{"path": "src/data/store.py", "old": "value = 1", "new": "value = 3"}]),
        ("deletions", ["src/data/store.py"]),
    ],
)
def test_patch_and_delete_also_require_guidance(field, value):
    data = project()
    generator = Generator(step(edits=[], **{field: value}))
    runner = Runner()
    result = worker.run_iteration(data, generator, runner, lambda: None)
    assert result["files"] == sorted(data["files"], key=lambda f: f["path"])
    assert result["focus_paths"] == ["AGENTS.md", "src/AGENTS.md", "src/data/AGENTS.md"]
    assert runner.calls == 0


def test_case_insensitive_name_and_directory_scope_boundary():
    files = [{"path": "src/agents.MD", "content": "Scoped."}]
    assert worker.project_guidance(files, ["src_extra/app.py"]) == []
    assert worker.project_guidance(files, ["src/new/app.py"])[0]["content"] == "Scoped."


def test_legacy_payload_omits_new_result_fields_during_worker_first_rollout():
    data = project()
    data.pop("guidance_version")
    assert "project_guidance" not in worker.model_context(data)
    generator = Generator(step(edits=[{"path": "src/data/store.py", "content": "value = 2\n"}]))
    result = worker.run_iteration(data, generator, Runner(), lambda: None)
    assert "guidance_reads" not in result
    assert not result["focus_paths"]


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_unknown_guidance_protocol_rejected(version):
    from project_contract import parse_payload

    data = project()
    data["guidance_version"] = version
    with pytest.raises(ProjectError, match="guidance version"):
        parse_payload({"required_skill": "code.build_project", "payload": data})


def test_guidance_exposes_addressed_patch_targets():
    data = project()
    data["focus_paths"] = ["src/AGENTS.md"]
    context = worker.model_context(data)
    spans = worker.addressed_patch_spans(context, data)
    address = next(value for value in spans.values() if value["path"] == "src/AGENTS.md")
    raw = {
        "patches": [
            {
                "path": "src/AGENTS.md",
                "span_id": address["span_id"],
                "new": "Use typed functions. Document migrations.",
            }
        ]
    }
    resolved = worker.resolve_model_patches(raw, spans)["patches"]
    generator = Generator(step(edits=[], patches=resolved))
    generator.last_guidance_reads = [
        {"path": g["path"], "sha256": g["sha256"]} for g in context["project_guidance"]
    ]
    result = worker.run_iteration(data, generator, Runner(), lambda: None)
    assert next(f["content"] for f in result["files"] if f["path"] == "src/AGENTS.md").endswith(
        "migrations."
    )
