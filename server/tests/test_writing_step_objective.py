from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.services.writing_contracts import WritingPayload
from app.services.writing_drafts import writing_payload


def test_step_objective_is_optional_and_preserves_the_authoritative_objective() -> None:
    objective = "Prepare a CRM implementation plan."
    conversation = [{"role": "user", "content": "Explain storage choices in French first."}]
    old = writing_payload(objective, conversation)
    assert "step_objective" not in old
    current = writing_payload(objective, conversation, step_objective="Compare SQLite tradeoffs.")
    assert current["objective"] == old["objective"]
    assert current["conversation"] == old["conversation"]
    assert current["step_objective"] == "Compare SQLite tradeoffs."
    assert WritingPayload.model_validate(current).model_dump(exclude_unset=True) == current


@pytest.mark.parametrize(
    "value", [None, 1, b"task", "", "  ", "a" * 4001, "bad\0text", "bad\ud800text"]
)
def test_step_objective_rejects_nontext_empty_invalid_unicode_and_overlong_values(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        WritingPayload.model_validate(
            {
                "schema_version": "1.0",
                "objective": "Write the CRM plan.",
                "conversation": [],
                "step_objective": value,
            }
        )


def test_step_objective_cannot_evict_user_context_or_exceed_existing_utf8_budget() -> None:
    objective = "é" * 4000
    conversation = [{"role": "user", "content": "é" * 4000} for _ in range(12)]
    old = writing_payload(objective, conversation)
    current = writing_payload(objective, conversation, step_objective="🧠" * 4000)
    assert current["objective"] == old["objective"]
    assert current["conversation"] == old["conversation"]
    assert len(json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode()) <= 32000
    assert WritingPayload.model_validate(current).model_dump(exclude_unset=True) == current


def test_step_objective_is_counted_in_the_entire_payload_byte_budget() -> None:
    value = {
        "schema_version": "1.0",
        "objective": "é" * 4000,
        "step_objective": "🧠" * 4000,
        "conversation": [{"role": "user", "content": "é" * 4000}],
    }
    with pytest.raises(ValidationError, match="byte limit"):
        WritingPayload.model_validate(value)
