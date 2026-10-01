"""Recovery output bounds must not authorize truncating an accepted source file."""

import ast
import copy
import json

import project_worker as worker
import pytest
from jsonschema import Draft202012Validator
from test_compact_repair_source import generator_64k, repair_payload
from test_project_model_budget import capture
from test_project_rejection_recovery import CONFLICT, install_transport, response
from test_project_worker import Runner, compact_step


def qa_payload():
    data = repair_payload()
    lines = data["files"][-1]["content"].splitlines(True)[:40]
    lines += [f"    assert {i} == {i}\n" for i in range(6)]
    lines += ["# éééé\n"] + ["# context\n"] * 35
    source = "".join(lines)
    lines[-1] = "# " + "x" * (2923 - len(source) + len(lines[-1]) - 3) + "\n"
    source = "".join(lines)
    assert len(source) == 2923 and len(source.encode()) == 2927
    assert len(source.splitlines()) == 82
    ast.parse(source)
    data["files"][-1]["content"] = source
    return data


def value_for(mode, **changes):
    return compact_step(**changes) if mode == "compact" else response(**changes)


def validate(mode, value, data):
    function = (
        worker.expand_compact_repair
        if mode == "compact"
        else worker.validate_bounded_rejection
    )
    return function(value, data)


@pytest.mark.parametrize("mode", ["compact", "bounded"])
def test_qa_syntax_valid_800_character_replacement_rejected_before_runner(
    monkeypatch, mode
):
    data = qa_payload()
    if mode == "bounded":
        data["conversation"][-1]["content"] = CONFLICT
    original = copy.deepcopy(data)
    replacement = "from selenium import webdriver\n# "
    replacement += "x" * (799 - len(replacement)) + "\n"
    assert len(replacement) == 800
    ast.parse(replacement)
    output = value_for(
        mode, edits=[{"path": data["files"][-1]["path"], "content": replacement}]
    )
    requests = install_transport(monkeypatch, output)
    runner = Runner()
    result = worker.run_iteration(data, generator_64k(), runner, lambda: None)
    assert len(requests) == 1 and runner.calls == 0
    assert (
        result["files"] == original["files"] and result["checks"] == original["checks"]
    )
    assert "compact repair exceeds its source limit" in result["message"]
    assert replacement not in result["message"]
    assert data == original
    followup = {
        **data,
        "conversation": [{"role": "assistant", "content": result["message"]}],
    }
    assert worker.follows_model_rejection(followup)
    body = capture(monkeypatch, generator_64k(), followup)
    assert all(
        branch["properties"]["plan"] == {"const": []}
        for branch in body["format"]["oneOf"]
    )
    assert all(
        branch["properties"]["edits"]["items"]["properties"]["content"]["maxLength"]
        == 800
        for branch in body["format"]["oneOf"]
    )


@pytest.mark.parametrize("mode", ["compact", "bounded"])
@pytest.mark.parametrize("characters", [800, 801])
def test_recovery_replacement_boundary_counts_characters_not_utf8_bytes(
    mode, characters
):
    data = qa_payload()
    data["files"][-1]["content"] = "é" * characters
    edit = {"path": data["files"][-1]["path"], "content": "é" * 800}
    value = value_for(mode, edits=[edit])
    if characters == 801:
        with pytest.raises(
            worker.ProjectError, match="compact repair exceeds its source limit"
        ):
            validate(mode, value, data)
    else:
        assert validate(mode, value, data)["edits"] == [edit]


@pytest.mark.parametrize("mode", ["compact", "bounded"])
@pytest.mark.parametrize("path", ["requirements.txt", "tests/test_new.py"])
def test_small_existing_and_new_files_stay_available_in_recovery(mode, path):
    data = qa_payload()
    value = value_for(
        mode, edits=[{"path": path, "content": "# Complete small file\n"}]
    )
    assert validate(mode, value, data)["edits"] == value["edits"]


def test_compact_schema_uses_patch_or_read_for_large_target_and_preserves_assertions(
    monkeypatch,
):
    data = qa_payload()
    source = data["files"][-1]
    replacement = "    options.add_argument('--headless')\n"

    def output(body):
        assert all(
            branch["properties"]["edits"]["maxItems"] == 0
            for branch in body["format"]["oneOf"]
        )
        assert '"not"' not in json.dumps(body["format"])
        metadata, _ = json.JSONDecoder().raw_decode(
            body["messages"][-1]["content"].removeprefix("Current workspace data:\n")
        )
        address = metadata["editable_spans"][0]
        assert address["path"] == source["path"]
        assert address["start_line"] == address["end_line"] == 40
        value = compact_step(
            edits=[],
            patches=[
                {
                    "path": source["path"],
                    "span_id": address["span_id"],
                    "new": replacement,
                }
            ],
        )
        assert Draft202012Validator(body["format"]).is_valid(value)
        return value

    requests = install_transport(monkeypatch, output)
    runner = Runner()
    result = worker.run_iteration(data, generator_64k(), runner, lambda: None)
    assert len(requests) == runner.calls == 1
    new_source = next(
        item["content"] for item in result["files"] if item["path"] == source["path"]
    )
    lines = source["content"].splitlines(True)
    assert new_source == "".join(lines[:39]) + replacement + "".join(lines[40:])
    assert (
        sum(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(new_source)))
        == 6
    )
    assert result["files"][:2] == data["files"][:2]


def test_compact_schema_small_target_keeps_complete_edit(monkeypatch):
    data = qa_payload()
    data["files"][-1]["content"] = "# context\n" * 39 + "VALUE = 1\n"
    body = capture(monkeypatch, generator_64k(), data)
    assert any(
        branch["properties"]["edits"].get("minItems") == 1
        for branch in body["format"]["oneOf"]
    )


def test_normal_mode_can_still_replace_large_existing_source(monkeypatch):
    data = qa_payload()
    data["conversation"] = []
    data["checks"] = []
    replacement = "# Complete intentional replacement\n" * 50
    output = response(
        edits=[{"path": data["files"][-1]["path"], "content": replacement}]
    )
    requests = install_transport(monkeypatch, output)
    step = generator_64k().generate(data)
    assert len(requests) == 1 and requests[0]["options"]["num_predict"] == 2000
    assert step["edits"] == output["edits"]
