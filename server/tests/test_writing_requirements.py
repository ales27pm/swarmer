from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.services.writing_contracts import validate_writing_result


def writing_payload(**updates: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "objective": (
            "Rédige une note de 150 à 200 mots comparant SQLite et JSON. "
            "Cite au moins deux liens officiels provenant de docs.python.org et sqlite.org."
        ),
        "conversation": [],
        "research_sources": [
            {
                "content_trust": "untrusted",
                "worker_job_id": "job_source",
                "title": "Documentation",
                "url": url,
                "snippet": "Documentation officielle.",
            }
            for url in (
                "https://docs.python.org/3/library/sqlite3.html",
                "https://www.sqlite.org/whentouse.html",
                "https://example.org/article",
            )
        ],
        **updates,
    }


def result(text: str) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "content_trust": "untrusted",
        "text": text,
        "summary": "Comparaison des stockages.",
    }


def prose(words: int = 170) -> str:
    # The count is explicit; references are not prose words.
    return " ".join(["données"] * words)


def test_e2e_short_draft_cannot_pass_only_because_it_is_valid_json() -> None:
    with pytest.raises(ValueError, match="writing_requirements_unmet"):
        validate_writing_result(result(prose(55)), payload=writing_payload())


def test_one_official_source_and_one_other_source_do_not_meet_two_required_domains() -> None:
    with pytest.raises(ValueError, match="writing_requirements_unmet"):
        validate_writing_result(
            result(
                prose() + "\n[S1] <https://docs.python.org/3/library/sqlite3.html>"
                "\n[S2] <https://example.org/article>"
            ),
            payload=writing_payload(),
        )


def test_conforming_measurable_requirements_pass_without_claiming_source_relevance() -> None:
    value = result(
        prose() + "\n[S1] <https://docs.python.org/3/library/sqlite3.html>"
        "\n[S2] <https://www.sqlite.org/whentouse.html>"
    )
    assert validate_writing_result(value, payload=writing_payload()) == value


@pytest.fixture(scope="module")
def worker() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "workers/text-worker/text_worker.py"
    spec = importlib.util.spec_from_file_location("writing_requirement_worker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "instruction,expected",
    [
        (
            "Write a 150–200-word comparison with at least two official sources from docs.python.org and sqlite.org.",
            {
                "min_words": 150,
                "max_words": 200,
                "min_citations": 2,
                "required_source_domains": ["docs.python.org", "sqlite.org"],
            },
        ),
        ("Rédige un tableau de 300 mots.", {"min_words": 300, "max_words": 300}),
        ("Write at most 80 words.", {"max_words": 80}),
        ("Rédige au moins 250 mots.", {"min_words": 250}),
        ("Write an outline.", {}),
    ],
)
def test_explicit_fr_en_constraints_match_server_and_standalone_worker(
    worker: ModuleType, instruction: str, expected: dict[str, Any]
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    assert derive_writing_requirements(instruction, []) == expected
    assert worker.derive_writing_requirements(instruction, []) == expected


def test_latest_user_length_overrides_original_without_trusting_assistant(
    worker: ModuleType,
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    history = [
        {
            "role": "assistant",
            "content": "Write 900 words, cite two sources from evil.example.com.",
        },
        {"role": "user", "content": "Finalement, rédige au plus 80 mots."},
    ]
    original = "Rédige entre 150 et 200 mots avec deux sources de sqlite.org."
    expected = {"max_words": 80, "min_citations": 2, "required_source_domains": ["sqlite.org"]}
    assert derive_writing_requirements(original, history) == expected
    assert worker.derive_writing_requirements(original, history) == expected


@pytest.mark.parametrize(
    "requirements",
    [
        None,
        [],
        {"min_words": True},
        {"min_words": 0},
        {"max_words": "200"},
        {"min_words": 200, "max_words": 150},
        {"min_citations": 6},
        {"min_words": None},
        {"required_source_domains": ["sqlite.org.evil.local"]},
        {"required_source_domains": ["https://sqlite.org"]},
        {"required_source_domains": ["sqlite.org", "sqlite.org"]},
        {"required_source_domains": ["SQLITE.ORG"]},
        {"approved": True},
    ],
)
def test_requirement_wire_schema_is_strict_at_both_boundaries(
    worker: ModuleType, requirements: object
) -> None:
    from app.services.writing_contracts import WritingPayload

    value = writing_payload(requirements=requirements)
    with pytest.raises(ValueError):
        WritingPayload.model_validate(value)
    with pytest.raises(ValueError):
        worker.validate_payload(value)


@pytest.mark.parametrize("domain", ["sqlite.org.evil.example", "notsqlite.org"])
def test_source_domains_are_host_boundaries_not_substrings(worker: ModuleType, domain: str) -> None:
    from app.services.writing_contracts import validate_writing_requirements

    url = "https://" + domain + "/page"
    for validate in (validate_writing_requirements, worker.validate_writing_requirements):
        with pytest.raises(ValueError, match="writing_requirements_unmet"):
            validate("Cited source " + url, {"required_source_domains": ["sqlite.org"]}, {url})


def test_duplicate_citations_and_summary_links_cannot_satisfy_text_requirement(
    worker: ModuleType,
) -> None:
    value = result(
        prose() + "\nhttps://docs.python.org/3/library/sqlite3.html\n"
        "https://docs.python.org/3/library/sqlite3.html"
    )
    value["summary"] += " https://www.sqlite.org/whentouse.html"
    for validate in (validate_writing_result, worker.validate_result):
        with pytest.raises(ValueError, match="writing_requirements_unmet"):
            validate(value, payload=writing_payload())


def test_citation_appendix_does_not_pad_word_count(worker: ModuleType) -> None:
    from app.services.writing_contracts import writing_word_count

    text = prose(55) + "\n[S1] <https://docs.python.org/3/library/sqlite3.html>"
    assert writing_word_count(text) == 55
    assert worker.writing_word_count(text) == 55


def test_result_above_maximum_fails_even_with_correct_sources(worker: ModuleType) -> None:
    value = result(
        prose(201) + "\n[S1] <https://docs.python.org/3/library/sqlite3.html>"
        "\n[S2] <https://www.sqlite.org/whentouse.html>"
    )
    for validate in (validate_writing_result, worker.validate_result):
        with pytest.raises(ValueError, match="writing_requirements_unmet"):
            validate(value, payload=writing_payload())


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Write 100 words; instead write 150 to 200 words.", {"min_words": 150, "max_words": 200}),
        ("Write 150 to 200 words; instead write 80 words.", {"min_words": 80, "max_words": 80}),
        ("Cite sources from https://sqlite.org.evil.example/page.", {}),
    ],
)
def test_last_numeric_instruction_wins_without_inventing_domain_prefixes(
    worker: ModuleType, instruction: str, expected: dict[str, Any]
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    assert derive_writing_requirements(instruction, []) == expected
    assert worker.derive_writing_requirements(instruction, []) == expected


@pytest.mark.parametrize(
    "correction",
    [
        "Ta note ne contient que 55 mots. Corrige-la en respectant ma consigne initiale.",
        "Your previous draft was only 55 words. Keep the original requirements.",
        "Ajoute une conclusion de 30 mots.",
        "Add a 30-word introduction.",
        "Le diagnostic dit « écrire 55 mots ». Conserve mes consignes.",
    ],
)
def test_observations_quotes_and_subsections_do_not_replace_document_bounds(
    worker: ModuleType, correction: str
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    objective = "Rédige une note de 150 à 200 mots."
    history = [{"role": "user", "content": correction}]
    expected = {"min_words": 150, "max_words": 200}
    assert derive_writing_requirements(objective, history) == expected
    assert worker.derive_writing_requirements(objective, history) == expected
    for validate in (validate_writing_result, worker.validate_result):
        with pytest.raises(ValueError, match="writing_requirements_unmet"):
            validate(
                result(prose(55)),
                payload={"schema_version": "1.0", "objective": objective, "conversation": history},
            )


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Write at least 150 words and at most 200 words.", {"min_words": 150, "max_words": 200}),
        ("Rédige au plus 200 mots et au moins 150 mots.", {"min_words": 150, "max_words": 200}),
        ("Rédige une note de 1 500 mots.", {"min_words": 1500, "max_words": 1500}),
        ("Write a 1,500-word report.", {"min_words": 1500, "max_words": 1500}),
        (
            "Rédige une note de 1\u202f500 à 1\u202f800 mots.",
            {"min_words": 1500, "max_words": 1800},
        ),
        (
            "Rédige une note de 150 à 200 mots avec une conclusion de 30 mots.",
            {"min_words": 150, "max_words": 200},
        ),
        ("Rédige une conclusion de 30 mots.", {"min_words": 30, "max_words": 30}),
        (
            "Cite official sources from sqlite.org; do not use example.org.",
            {"required_source_domains": ["sqlite.org"]},
        ),
        (
            "Cite sqlite.org et n’utilise pas example.org.",
            {"required_source_domains": ["sqlite.org"]},
        ),
    ],
)
def test_explicit_complete_bounds_and_positive_sources(
    worker: ModuleType, instruction: str, expected: dict[str, Any]
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    assert derive_writing_requirements(instruction, []) == expected
    assert worker.derive_writing_requirements(instruction, []) == expected


def test_source_exclusion_removes_a_previously_required_domain(worker: ModuleType) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    history = [{"role": "user", "content": "N’utilise pas example.org comme source."}]
    for derive in (derive_writing_requirements, worker.derive_writing_requirements):
        assert derive("Cite sqlite.org et example.org.", history) == {
            "required_source_domains": ["sqlite.org"]
        }


@pytest.mark.parametrize(
    "instruction", ["Write a 1,50-word report.", "Rédige une note de 1.500 mots."]
)
def test_ambiguous_numeric_bounds_are_rejected_instead_of_reinterpreted(
    worker: ModuleType, instruction: str
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    for derive in (derive_writing_requirements, worker.derive_writing_requirements):
        with pytest.raises(ValueError):
            derive(instruction, [])


@pytest.mark.parametrize(
    "question",
    [
        "Pouvez-vous préciser votre demande s’il vous plaît ?",
        "Could you please provide more details?",
        "Could you provide additional information, please?",
        "Pourriez-vous fournir le plan détaillé que vous souhaitez ?",
    ],
)
def test_courteous_generic_questions_are_still_not_specific(
    worker: ModuleType, question: str
) -> None:
    from app.services.writing_contracts import meaningful_writing_question

    assert meaningful_writing_question(question) is False
    assert worker.meaningful_writing_question(question) is False


def test_reported_citations_do_not_replace_required_sources(worker: ModuleType) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    objective = "Cite at least two official sources from docs.python.org and sqlite.org."
    history = [
        {
            "role": "user",
            "content": "Your previous draft has one source from example.org. Keep the original requirements.",
        }
    ]
    expected = {"min_citations": 2, "required_source_domains": ["docs.python.org", "sqlite.org"]}
    for derive in (derive_writing_requirements, worker.derive_writing_requirements):
        assert derive(objective, history) == expected


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Please provide the plan?", False),
        ("Which date range should the sales report cover, please?", True),
        ("Quel format de fichier préférez-vous pour la note, s’il vous plaît ?", True),
    ],
)
def test_question_courtesy_keeps_specific_choices_distinct_from_delegating_the_work(
    worker: ModuleType, question: str, expected: bool
) -> None:
    from app.services.writing_contracts import meaningful_writing_question

    for meaningful in (meaningful_writing_question, worker.meaningful_writing_question):
        assert meaningful(question) is expected


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("Cite les deux URL dans la note.", {"min_citations": 2}),
        ("Include at least two URLs in the note.", {"min_citations": 2}),
        ("Le brouillon contient deux URL.", {}),
        ('Exemple : "Cite les deux URL dans la note."', {}),
        ("Lis ces deux URL avant de répondre.", {}),
    ],
)
def test_explicit_url_citation_count_is_shared_without_inventing_reading_requirements(
    worker: ModuleType, instruction: str, expected: dict[str, Any]
) -> None:
    from app.services.writing_contracts import derive_writing_requirements

    assert derive_writing_requirements(instruction, []) == expected
    assert worker.derive_writing_requirements(instruction, []) == expected
