from __future__ import annotations

from types import ModuleType

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_text_worker import draft, payload, research_source
from test_text_worker import worker as worker  # noqa: PLC0414


def model_schema(worker: ModuleType, sourced: bool) -> Draft202012Validator:
    request = payload()
    if sourced:
        request["research_sources"] = [research_source()]
    _, schema = worker._model_input(worker.validate_payload(request))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("sourced", [False, True])
@pytest.mark.parametrize(
    "outcome", ["delivered", "declined", "needs_clarification", "insufficient_sources"]
)
def test_generation_schema_accepts_each_structural_outcome(
    worker: ModuleType, sourced: bool, outcome: str
) -> None:
    response = {**draft(), "outcome": outcome}
    if outcome == "needs_clarification":
        response["question"] = "Quelle année faut-il utiliser pour ce calendrier ?"
    model_schema(worker, sourced).validate(response)


@pytest.mark.parametrize("sourced", [False, True])
@pytest.mark.parametrize(
    "outcome", ["delivered", "declined", "needs_clarification", "insufficient_sources"]
)
def test_generation_schema_rejects_question_presence_inconsistent_with_outcome(
    worker: ModuleType, sourced: bool, outcome: str
) -> None:
    response = {**draft(), "outcome": outcome}
    if outcome != "needs_clarification":
        response["question"] = "Quelle année faut-il utiliser pour ce calendrier ?"
    with pytest.raises(ValidationError):
        model_schema(worker, sourced).validate(response)


@pytest.mark.parametrize("outcome", ["declined", "needs_clarification", "insufficient_sources"])
def test_non_delivery_cannot_select_decorative_sources(worker: ModuleType, outcome: str) -> None:
    response = {**draft(), "outcome": outcome, "source_ids": ["S1"]}
    if outcome == "needs_clarification":
        response["question"] = "Quelle année faut-il utiliser pour ce calendrier ?"
    with pytest.raises(ValidationError):
        model_schema(worker, True).validate(response)
