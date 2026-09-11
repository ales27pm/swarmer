from __future__ import annotations

import copy

import project_contract as contract
import pytest


def payload() -> dict:
    return {
        "objective": "Créer une application",
        "conversation": [],
        "files": [],
        "plan": [],
        "checks": [],
        "iteration": 1,
        "base_revision_id": None,
        "base_sha256": None,
    }


def step(**changes: object) -> dict:
    return {
        "action": "continue",
        "message": "Apply changes",
        "plan": ["Implement project"],
        "edits": [],
        "patches": [],
        "deletions": [],
        "requested_checks": [],
        "run_instructions": "Run tests",
        "runtime": "python",
        **changes,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../a.py",
        "a/../b.py",
        "a//b",
        "./app.py",
        "a\\b",
        "a\0b",
        ".git/config",
        ".env",
        ".npmrc",
        "id_ed25519",
        "a.key",
        "a.p12",
        "node_modules/x.js",
        "auth.json",
        "x/credentials.json",
    ],
)
def test_untrusted_paths_cannot_address_credentials_or_escape(path: str) -> None:
    with pytest.raises(contract.ProjectError):
        contract.files_value([{"path": path, "content": "x"}])


@pytest.mark.parametrize(
    "paths", [["app.py", "APP.py"], ["app", "app/main.py"], ["app/main.py", "App"]]
)
def test_snapshot_rejects_case_and_directory_collisions(paths: list[str]) -> None:
    with pytest.raises(contract.ProjectError):
        contract.files_value([{"path": path, "content": "x"} for path in paths])


def test_utf8_and_control_bounds_are_checked_before_materialization() -> None:
    for content in ("é" * 32_001, "x\0y", "x\x1by", "\ud800"):
        with pytest.raises(contract.ProjectError):
            contract.files_value([{"path": "app.py", "content": content}])
    assert contract.files_value([{"path": "empty.txt", "content": ""}])
    with pytest.raises(contract.ProjectError):
        contract.files_value([{"path": f"file{i}.txt", "content": "x" * 64_000} for i in range(16)])


def test_unchanged_files_survive_incremental_repair_and_base_hash_is_verified() -> None:
    files = [{"path": "README.md", "content": "Keep me"}, {"path": "app.py", "content": "bad"}]
    original = copy.deepcopy(files)
    merged = contract.merge_files(files, step(edits=[{"path": "app.py", "content": "fixed"}]))
    assert files == original
    assert merged == [
        {"path": "README.md", "content": "Keep me"},
        {"path": "app.py", "content": "fixed"},
    ]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "revision_1",
        "base_sha256": contract.snapshot_sha(files),
    }
    assert (
        contract.parse_payload({"required_skill": contract.SKILL, "payload": data})["files"]
        == files
    )
    data["files"] = merged
    with pytest.raises(contract.ProjectError, match="digest"):
        contract.parse_payload({"required_skill": contract.SKILL, "payload": data})


def test_deletions_are_explicit_and_cannot_target_absent_files() -> None:
    files = [{"path": "README.md", "content": "keep"}, {"path": "old.py", "content": "old"}]
    assert contract.merge_files(files, step(deletions=["old.py"])) == files[:1]
    with pytest.raises(contract.ProjectError):
        contract.merge_files(files, step(deletions=["not-here.py"]))


def test_exact_patches_preserve_unseen_bytes_and_apply_unicode_offsets_from_the_base() -> None:
    files = [{"path": "app.py", "content": "# Montréal\nfirst = 1\nlast = 2\n# Fin été\n"}]
    original = copy.deepcopy(files)
    merged = contract.merge_files(
        files,
        step(
            patches=[
                {"path": "app.py", "old": "first = 1", "new": "first = 12345"},
                {"path": "app.py", "old": "last = 2", "new": "last = 3"},
            ]
        ),
    )
    assert merged[0]["content"] == "# Montréal\nfirst = 12345\nlast = 3\n# Fin été\n"
    assert files == original
    data = {
        **payload(),
        "files": merged,
        "base_revision_id": "r1",
        "base_sha256": contract.snapshot_sha(original),
    }
    with pytest.raises(contract.ProjectError, match="digest"):
        contract.parse_payload({"required_skill": contract.SKILL, "payload": data})


@pytest.mark.parametrize("old", ["missing", "same"])
def test_patch_absent_or_ambiguous_match_rejects_entire_batch(old: str) -> None:
    files = [{"path": "app.py", "content": "unique same same"}]
    original = copy.deepcopy(files)
    with pytest.raises(contract.ProjectError, match="exactly once"):
        contract.merge_files(
            files,
            step(
                patches=[
                    {"path": "app.py", "old": "unique", "new": "changed"},
                    {"path": "app.py", "old": old, "new": "replacement"},
                ]
            ),
        )
    assert files == original


def test_overlapping_patch_ranges_are_rejected_atomically() -> None:
    files = [{"path": "app.py", "content": "abcde"}]
    with pytest.raises(contract.ProjectError, match="overlap"):
        contract.merge_files(
            files,
            step(
                patches=[
                    {"path": "app.py", "old": "abc", "new": "x"},
                    {"path": "app.py", "old": "bcd", "new": "y"},
                ]
            ),
        )
    assert files[0]["content"] == "abcde"


@pytest.mark.parametrize(
    "changes",
    [
        {"edits": [{"path": "app.py", "content": "replacement"}]},
        {"deletions": ["app.py"]},
        {"edits": [{"path": "APP.py", "content": "replacement"}]},
    ],
)
def test_patch_cannot_share_a_path_with_replacement_or_deletion(changes: dict) -> None:
    with pytest.raises(contract.ProjectError, match="conflicts"):
        contract.parse_step(step(patches=[{"path": "app.py", "old": "a", "new": "b"}], **changes))


@pytest.mark.parametrize("old,new", [("", "x"), ("a", "a"), ("a", "é" * 4001), ("a", "\x1b")])
def test_patch_text_is_nonempty_distinct_and_byte_bounded(old: str, new: str) -> None:
    with pytest.raises(contract.ProjectError):
        contract.parse_step(step(patches=[{"path": "app.py", "old": old, "new": new}]))


def test_patch_count_and_changed_path_budget_include_all_change_kinds() -> None:
    with pytest.raises(contract.ProjectError, match="patch count"):
        contract.parse_step(step(patches=[{"path": "app.py", "old": "a", "new": "b"}] * 9))
    with pytest.raises(contract.ProjectError, match="three changed paths"):
        contract.parse_step(
            step(
                edits=[{"path": "new.py", "content": "new"}],
                deletions=["old.py"],
                patches=[{"path": name, "old": "a", "new": "b"} for name in ("a.py", "b.py")],
            )
        )


@pytest.mark.parametrize("size,count", [(64_000, 1), (62_500, 16)])
def test_patch_cumulative_size_overflow_keeps_the_original_snapshot(size: int, count: int) -> None:
    files = [{"path": f"file{i}.txt", "content": "a" * (size - 1) + "Z"} for i in range(count)]
    original = copy.deepcopy(files)
    with pytest.raises(contract.ProjectError, match="limit"):
        contract.merge_files(files, step(patches=[{"path": "file0.txt", "old": "Z", "new": "ZZ"}]))
    assert files == original


def test_project_payload_rejects_worker_retargeting_fields() -> None:
    for changes in (
        {"url": "http://remote"},
        {"model": "remote"},
        {"iteration": True},
        {"base_revision_id": "revision_1"},
    ):
        with pytest.raises(contract.ProjectError):
            contract.parse_payload(
                {"required_skill": contract.SKILL, "payload": {**payload(), **changes}}
            )


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, 2.0])
def test_memory_cannot_exceed_its_typed_bounds(score: object) -> None:
    with pytest.raises(contract.ProjectError):
        contract.memory_value(
            {
                "mode": "semantic",
                "reason": "matched",
                "items": [{"id": "m1", "summary": "hint", "score": score, "source_id": "r1"}],
            }
        )


def test_worker_digest_and_result_agree_with_control_plane_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "server"))
    from app.services.project_contracts import ProjectPayload, project_digest

    files = [{"path": "a.py", "content": "# café\n"}, {"path": "README.md", "content": "Test"}]
    data = {
        **payload(),
        "files": files,
        "base_revision_id": "revision_1",
        "base_sha256": contract.snapshot_sha(files),
    }
    assert contract.snapshot_sha(files) == project_digest(files)
    assert ProjectPayload.model_validate(data).base_sha256 == data["base_sha256"]
