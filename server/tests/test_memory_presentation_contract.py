from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


@pytest.fixture
def validator() -> Draft202012Validator:
    spec = yaml.safe_load((Path(__file__).resolve().parents[2] / "api/openapi.yaml").read_text())
    return Draft202012Validator(spec["components"]["schemas"]["MemoryPresentation"])


def display(original: bool) -> dict:
    value = {
        "language": "fr",
        "content": "Conserver le fichier.",
        "summary": None,
        "canonical_sha256": "a" * 64,
        "summary_sha256": None,
        "source_revision": "revision-1",
        "validation_status": "model_reviewed",
        "temporary": True,
        "grants_authority": False,
    }
    if original:
        value.update(
            mode="original",
            validation_status="source_preserved",
            source_id="msrc_1",
            source_sha256="b" * 64,
            canonical_receipt_id="receipt_1",
        )
    return value


@pytest.mark.parametrize("original", [False, True])
def test_both_display_contracts_are_explicit(validator, original):
    validator.validate(display(original))


@pytest.mark.parametrize("field", ["mode", "source_id", "source_sha256", "canonical_receipt_id"])
def test_original_without_binding_is_invalid(validator, field):
    value = display(True)
    del value[field]
    assert list(validator.iter_errors(value))


@pytest.mark.parametrize(
    "original,change",
    [
        (False, {"mode": "original"}),
        (False, {"validation_status": "source_preserved"}),
        (True, {"validation_status": "model_reviewed"}),
        (True, {"source_sha256": "not-a-hash"}),
        (True, {"grants_authority": True}),
        (True, {"canonical_receipt_id": ""}),
    ],
)
def test_no_mode_or_authority_confusion(validator, original, change):
    assert list(validator.iter_errors({**display(original), **change}))
