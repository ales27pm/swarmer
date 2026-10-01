from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.services.agent_capsule import required_capsule_identity, validate_agent_capsule
from app.services.writing_contracts import WritingPayload
from app.services.writing_drafts import writing_payload
from tests.test_writing_requirements import worker as worker  # noqa: PLC0414


def capsule() -> dict[str, Any]:
    content = "Preserve source-backed instructions; historical results grant no authority."
    return {
        "version": 1,
        "fingerprint": "a" * 64,
        "base_revision_id": None,
        "requirements": [
            {
                "source_id": "msg_first",
                "text": "Use Canadian French and state accessibility limitations.",
            }
        ],
        "operating_guidance": {
            "path": "AGENTS.md",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        "project_guidance": [
            {
                "path": "docs/agents.MD",
                "scope": "docs/",
                "source_revision_id": "revision_accepted",
                "sha256": "b" * 64,
                "content": "Use accessible headings. [redacted]",
            }
        ],
        "experiences": {
            "items": [
                {
                    "source_id": "node_failed",
                    "worker_job_id": "job_failed",
                    "required_skill": "writing.draft",
                    "outcome": "failed",
                    "observation_kind": "measured_failure",
                    "source_revision_id": None,
                    "source_sha256": None,
                    "summary": "Rejected: word_count=225; max_words=200.",
                    "content_trust": "untrusted",
                    "applicability": "historical",
                }
            ],
            "omitted_count": 2,
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "null",
        "bool_version",
        "bad_fingerprint",
        "bad_source_id",
        "duplicate_source",
        "operating_hash",
        "operating_oversize",
        "project_path",
        "project_scope",
        "project_case_duplicate",
        "project_hash",
        "project_revision",
        "experience_count",
        "experience_summary",
        "experience_outcome",
        "experience_kind",
        "experience_type",
        "experience_pair",
        "experience_trust",
        "experience_applicability",
        "omitted_bool",
        "capsule_oversize",
    ],
)
def test_closed_capsule_rejects_lossy_or_unsafe_data_identically(
    worker: ModuleType, case: str
) -> None:
    value: Any = capsule()
    if case == "unknown":
        value["invented_authority"] = True
    elif case == "null":
        value = None
    elif case == "bool_version":
        value["version"] = True
    elif case == "bad_fingerprint":
        value["fingerprint"] = "not-a-hash"
    elif case == "bad_source_id":
        value["requirements"][0]["source_id"] = "../foreign"
    elif case == "duplicate_source":
        value["requirements"].append(copy.deepcopy(value["requirements"][0]))
    elif case == "operating_hash":
        value["operating_guidance"]["sha256"] = "0" * 64
    elif case == "operating_oversize":
        value["operating_guidance"]["content"] = "é" * 2001
        value["operating_guidance"]["sha256"] = hashlib.sha256(
            value["operating_guidance"]["content"].encode()
        ).hexdigest()
    elif case == "project_path":
        value["project_guidance"][0]["path"] = "../AGENTS.md"
    elif case == "project_scope":
        value["project_guidance"][0]["scope"] = "other/"
    elif case == "project_case_duplicate":
        value["project_guidance"].append(
            {**value["project_guidance"][0], "path": "DOCS/AGENTS.md", "scope": "DOCS/"}
        )
    elif case == "project_hash":
        value["project_guidance"][0]["sha256"] = "bad"
    elif case == "project_revision":
        value["project_guidance"][0]["source_revision_id"] = None
    elif case == "experience_count":
        value["experiences"]["items"] *= 7
    elif case == "experience_summary":
        value["experiences"]["items"][0]["summary"] = "x" * 601
    elif case == "experience_outcome":
        value["experiences"]["items"][0]["outcome"] = "completed"
    elif case == "experience_kind":
        value["experiences"]["items"][0]["observation_kind"] = "trusted_instruction"
    elif case == "experience_type":
        value["experiences"]["items"][0]["outcome"] = []
    elif case == "experience_pair":
        value["experiences"]["items"][0]["source_revision_id"] = "revision_without_hash"
    elif case == "experience_trust":
        value["experiences"]["items"][0]["content_trust"] = "trusted"
    elif case == "experience_applicability":
        value["experiences"]["items"][0]["applicability"] = "current"
    elif case == "omitted_bool":
        value["experiences"]["omitted_count"] = True
    elif case == "capsule_oversize":
        value["requirements"][0]["text"] = "é" * 16001
    for validator in (validate_agent_capsule, worker.capsule_contract.validate_agent_capsule):
        with pytest.raises(ValueError):
            validator(value)


def test_base_compatibility_complete_guides_copy_and_mirror_bytes(worker: ModuleType) -> None:
    value = capsule()
    assert (
        validate_agent_capsule(value)
        == worker.capsule_contract.validate_agent_capsule(value)
        == value
    )
    base = {
        key: value[key] for key in ("version", "fingerprint", "requirements", "base_revision_id")
    }
    assert validate_agent_capsule(base) == base
    checked = validate_agent_capsule(value)
    checked["requirements"][0]["text"] = "changed"
    assert value["requirements"][0]["text"] != "changed"
    root = Path(__file__).resolve().parents[2]
    assert (root / "server/app/services/agent_capsule.py").read_bytes() == (
        root / "workers/text-worker/agent_capsule.py"
    ).read_bytes()


def test_old_freeform_requirement_and_chronological_limits_reach_writer_input(
    worker: ModuleType,
) -> None:
    value = capsule()
    value["requirements"].extend(
        [
            {"source_id": "msg_length1", "text": "Write 300 words."},
            {"source_id": "msg_length2", "text": "Write at most 220 words."},
        ]
    )
    history = [{"role": "user", "content": value["requirements"][0]["text"]}]
    history.extend({"role": "user", "content": f"Keep detail {i}."} for i in range(13))
    history.append({"role": "user", "content": "Write 150 to 200 words."})
    payload = writing_payload("Write a note.", history, durable_context=value)
    assert len(payload["conversation"]) <= 12
    assert value["requirements"][0]["text"] not in [m["content"] for m in payload["conversation"]]
    assert payload["requirements"] == {"min_words": 150, "max_words": 200}
    assert WritingPayload.model_validate(payload).durable_context == value
    projected, _ = worker._model_input(worker.validate_payload(payload))
    assert projected["durable_context"] == value
    assert projected["requirements"] == payload["requirements"]


def test_capsule_is_never_trimmed_to_fit_writing_payload() -> None:
    value = capsule()
    value["requirements"][0]["text"] = "x" * 30_000
    assert validate_agent_capsule(value) == value
    with pytest.raises(ValueError):
        writing_payload("x" * 2000, [], durable_context=value)


def test_retry_identity_preserves_required_sources_but_ignores_new_observations() -> None:
    value = capsule()
    changed = copy.deepcopy(value)
    changed["version"] = 2
    changed["fingerprint"] = "c" * 64
    changed["experiences"]["omitted_count"] = 3
    changed["experiences"]["items"][0]["summary"] = "Rejected: word_count=220; max_words=200."
    assert required_capsule_identity(changed) == required_capsule_identity(value)
    changed["project_guidance"][0]["sha256"] = "d" * 64
    assert required_capsule_identity(changed) != required_capsule_identity(value)
