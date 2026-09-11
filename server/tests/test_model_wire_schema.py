from copy import deepcopy
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from app.services.model_wire_schema import model_wire_schema
from app.services.swarm_contracts import EvaluationDecision, SwarmPlanProposal


@pytest.mark.parametrize("model", [SwarmPlanProposal, EvaluationDecision])
def test_wire_schema_preserves_authoritative_schema_and_other_constraints(
    model: type[BaseModel],
) -> None:
    source = model.model_json_schema()
    original = deepcopy(source)
    with patch.object(model, "model_json_schema", return_value=source):
        wire = model_wire_schema(model)

    assert source == original
    assert wire is not source
    assert wire["additionalProperties"] is False
    assert wire["required"] == source["required"]
    node = wire["$defs"]["SwarmPlanNodeProposal"]["properties"]
    assert node["objective"]["minLength"] == 1
    assert "maxLength" not in node["objective"]
    assert source["$defs"]["SwarmPlanNodeProposal"]["properties"]["objective"]["maxLength"] == 4000
    assert node["temporary_id"]["pattern"] == "^[A-Za-z][A-Za-z0-9_-]*$"
    assert node["dependencies"]["maxItems"] == 20
    assert node["priority"]["minimum"] == 0
    assert node["priority"]["maximum"] == 100
    assert node["required_skill"]["anyOf"][1] == {"type": "null"}
    # Mutating the wire schema cannot change cached/caller-owned source data.
    node["objective"]["minLength"] = 0
    assert source == original
