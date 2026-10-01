"""Bounded textual drafts; producing text grants no execution authority."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.services.agent_capsule import validate_agent_capsule

WRITING_SKILL = "writing.draft"
MAX_WRITING_PAYLOAD_BYTES = 32_000
MAX_WRITING_TEXT_BYTES = 24_000
MAX_WRITING_SUMMARY_CHARACTERS = 1_200
MAX_WRITING_CONTEXT_CHARACTERS = 4_000
MAX_WRITING_CONVERSATION_MESSAGES = 12
MAX_RESEARCH_SOURCES = 6
MAX_RESEARCH_SOURCE_BYTES = 24_000
MAX_DEPENDENCY_ITEMS = 8
MAX_DEPENDENCY_BYTES = 12_000


def checked_research_url(value: str) -> str:
    """Validate a citation without fetching it or rewriting its destination."""
    value.encode("utf-8")
    if (
        not value
        or len(value) > 1_000
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("research URL is outside its bounds")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or host.lower() == "localhost"
        or host.lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("research URL must be a credential-free public HTTP(S) URL")
    _ = parsed.port
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or "\\" in host or "%" in host:
            raise ValueError("research URL hostname is invalid") from None
    else:
        if not address.is_global:
            raise ValueError("research URL must not identify a private address")
    return value


def _checked_text(value: str, *, max_bytes: int | None = None) -> str:
    if not value.strip() or "\0" in value:
        raise ValueError("writing text must be nonempty and contain no NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("writing text must contain valid Unicode") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError("writing text exceeds its UTF-8 byte limit")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WritingConversationMessage(_StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return _checked_text(value)


class PageExcerptEvidence(_StrictModel):
    """A read receipt, not an assertion that the excerpt satisfies the task."""

    kind: Literal["page_excerpt"]
    requested_url: str = Field(min_length=1, max_length=1_000)
    final_url: str = Field(min_length=1, max_length=1_000)
    fetched_at: str = Field(min_length=1, max_length=64)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=4_000)
    truncated: bool

    @field_validator("requested_url", "final_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        checked_research_url(value)
        if urlsplit(value).scheme != "https" or urlsplit(value).port not in (None, 443):
            raise ValueError("page evidence requires public HTTPS")
        return value

    @field_validator("fetched_at")
    @classmethod
    def validate_time(cls, value: str) -> str:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("page timestamp needs a timezone")
        return value

    @model_validator(mode="after")
    def validate_excerpt(self) -> PageExcerptEvidence:
        _checked_text(self.text)
        if hashlib.sha256(self.text.encode()).hexdigest() != self.excerpt_sha256:
            raise ValueError("page excerpt digest mismatch")
        return self


class WritingResearchSource(_StrictModel):
    content_trust: Literal["untrusted"]
    worker_job_id: str = Field(min_length=5, max_length=200)
    title: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=1_000)
    snippet: str = Field(max_length=700)
    evidence: PageExcerptEvidence | None = None

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: PageExcerptEvidence | None) -> PageExcerptEvidence:
        if value is None:
            raise ValueError("page evidence must be an object when provided")
        return value

    @model_validator(mode="after")
    def validate_citation(self) -> WritingResearchSource:
        if self.evidence is not None and self.url != self.evidence.final_url:
            raise ValueError("page citation does not match its final URL")
        return self

    @model_serializer(mode="wrap")
    def serialize_source(self, handler: Any) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.evidence is None:
            result.pop("evidence", None)
        return result

    @field_validator("worker_job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if re.fullmatch(r"job_[A-Za-z0-9._:-]+", value) is None:
            raise ValueError("research source job identity is invalid")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _checked_text(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return checked_research_url(value)

    @field_validator("snippet")
    @classmethod
    def validate_snippet(cls, value: str) -> str:
        if value:
            return _checked_text(value)
        return value


def research_source_limits(sources: list[dict[str, Any]]) -> tuple[int, int]:
    # The richer budget is reserved for actually read passages; legacy search
    # snippets retain their original prompt budget and wire bounds.
    return (6, 24_000) if any(item.get("evidence") for item in sources) else (5, 8_000)


class DependencyContextItem(_StrictModel):
    content_trust: Literal["untrusted"]
    node_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    worker_job_id: str = Field(pattern=r"^job_[A-Za-z0-9._:-]+$", max_length=200)
    required_skill: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2_000)

    @field_validator("summary", "required_skill")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _checked_text(value)


class WritingRequirements(_StrictModel):
    """Measurable constraints; passing these does not establish factual relevance."""

    min_words: int | None = Field(default=None, ge=1, le=100_000)
    max_words: int | None = Field(default=None, ge=1, le=100_000)
    min_citations: int | None = Field(default=None, ge=0, le=5)
    required_source_domains: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("min_words", "max_words", "min_citations")
    @classmethod
    def reject_explicit_null(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("writing requirement must be an integer when provided")
        return value

    @field_validator("required_source_domains")
    @classmethod
    def validate_domains(cls, value: list[str]) -> list[str]:
        for domain in value:
            if (
                len(domain) > 253
                or domain != domain.lower()
                or re.fullmatch(
                    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                    r"[a-z]{2,63}",
                    domain,
                )
                is None
            ):
                raise ValueError("source domain must be a lowercase public hostname")
            checked_research_url("https://" + domain)
        if len(set(value)) != len(value):
            raise ValueError("source domains must be distinct")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> WritingRequirements:
        if (
            self.min_words is not None
            and self.max_words is not None
            and self.min_words > self.max_words
        ):
            raise ValueError("minimum word count exceeds maximum")
        return self


# Kept identical in the standalone text worker; boundary tests exercise both.
_WRITING_NUMBER = r"\d+(?:[ ,.\u00a0\u202f]\d+)*"
_WRITING_SUBJECT = re.compile(
    r"\b(note|document|rapport|report|article|texte|text|tableau|table|"
    r"réponse|response|draft|conclusion|introduction|paragraph|paragraphe|section|"
    r"résumé|summary|abstract)\b"
)
_WRITING_SECTIONS = {"conclusion", "introduction", "paragraph", "paragraphe", "section"}
_WRITING_DIRECTIVE = re.compile(
    r"\b(?:write|draft|compose|produce|provide|include|cite|use|rédig\w*|écri\w*|"
    r"produis\w*|fournis\w*|inclu\w*|citez|utilis\w*|doit|doivent|must|should|search|research|recherch\w*|effectue)\b"
)
_WRITING_OBSERVATION = re.compile(
    r"\b(?:was|were|contains?|contained|has|had|said|says|asked|requested|"
    r"contient|contenait|comptait|fait|faisait|dit|demandé|demandais)\b"
)


def _writing_instruction_text(text: str) -> str:
    # Quoted diagnostics, examples and code are not new user requirements.
    return re.sub(
        r'```[\s\S]*?```|`[^`]*`|«[^»]*»|“[^”]*”|"[^"\n]*"',
        lambda match: " " * len(match[0]),
        text.casefold(),
    )


def _writing_clause_prefix(text: str, start: int) -> str:
    return re.split(r"[;\n]|[.!?](?:\s+|$)", text[:start])[-1]


def _writing_count_is_directive(text: str, start: int) -> bool:
    prefix = _writing_clause_prefix(text, start)
    directives = list(_WRITING_DIRECTIVE.finditer(prefix))
    observations = list(_WRITING_OBSERVATION.finditer(prefix))
    if observations and (not directives or observations[-1].start() > directives[-1].start()):
        return False
    if directives:
        return True
    # Support terse bounds such as "150 words" or "At most 200 words".
    # Unrecognized natural-language statements remain context, not a new contract.
    return (
        re.fullmatch(
            r"\s*(?:(?:please|finalement|instead|maximum|minimum|length|longueur)[:,]?\s*)?"
            r"(?:(?:at most|at least|no more than|au plus|au moins|entre|between)\s*)?",
            prefix,
        )
        is not None
    )


def _writing_integer(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+", value):
        return int(re.sub(r"[ ,\u00a0\u202f]", "", value))
    # In particular, do not reinterpret 1,50 or 1.500 as a 50/500-word request.
    raise ValueError("ambiguous writing word count")


def _writing_document_bound(text: str, start: int, end: int, primary: str | None) -> bool:
    before = list(_WRITING_SUBJECT.finditer(_writing_clause_prefix(text, start)))
    after = _WRITING_SUBJECT.match(text[end:].lstrip(" -"))
    subject = after[1] if after else (before[-1][1] if before else None)
    return subject not in _WRITING_SECTIONS or subject == primary


def _writing_source_domains(text: str) -> tuple[list[str], set[str]]:
    positive: list[str] = []
    excluded: set[str] = set()
    if not re.search(
        r"\b(?:sources?|cite|citez|citations?|documentation|research|recherche)\b", text
    ):
        return positive, excluded
    for match in re.finditer(
        r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
        r"(?:org|com|net|edu|gov|io|dev|ca))(?=[/:\s,;)]|[.](?:\s|$)|$)",
        text,
    ):
        prefix = _writing_clause_prefix(text, match.start())
        # A later positive directive ("but cite ...") ends a negative clause.
        prefix = re.split(r"\b(?:but|mais)\b", prefix)[-1]
        negative = re.search(
            r"\b(?:do not|don't|never|avoid|exclude|excluding|without|except|"
            r"sans|sauf|hors|exclu\w*|évite\w*|n['’]\w+\s+pas|ne\s+\w+\s+pas)\b",
            prefix,
        )
        domain = match[1]
        if negative:
            excluded.add(domain)
        elif domain not in positive and _writing_count_is_directive(text, match.start()):
            positive.append(domain)
    return positive, excluded


def derive_writing_requirements(
    objective: str, conversation: list[dict[str, Any]]
) -> dict[str, Any]:
    """Extract a conservative explicit FR/EN subset, never infer full NL compliance.

    Latest document directives replace older word bounds. Observations, quoted
    examples and subordinate-section lengths do not redefine the whole document.
    Independent min/max directives in the same message are combined. Assistant
    text, planner steps and search snippets never supply binding constraints.
    """
    result: dict[str, Any] = {}
    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "un": 1,
        "une": 1,
        "deux": 2,
        "trois": 3,
        "quatre": 4,
        "cinq": 5,
    }
    initial = _WRITING_SUBJECT.search(_writing_instruction_text(objective))
    primary = initial[1] if initial else None
    texts = [objective, *(m["content"] for m in conversation if m.get("role") == "user")]
    count_pattern = (
        rf"(?<![\w,.])({_WRITING_NUMBER})(?:\s*(?:à|et|to|and|[-–—])\s*"
        rf"({_WRITING_NUMBER}))?\s*[- ]?\s*(?:mots?|words?)\b"
    )
    for text in texts:
        lower = _writing_instruction_text(text)
        bounds: dict[str, int] = {}
        for match in re.finditer(count_pattern, lower):
            if not _writing_count_is_directive(lower, match.start()):
                continue
            if not _writing_document_bound(lower, match.start(), match.end(), primary):
                continue
            count = _writing_integer(match[1])
            if match[2] is not None:
                bounds = {"min_words": count, "max_words": _writing_integer(match[2])}
                continue
            prefix = lower[: match.start()]
            if re.search(r"(?:at most|no more than|maximum|au plus|jusqu['’]à)\s*$", prefix):
                bounds["max_words"] = count
            elif re.search(r"(?:at least|minimum|au moins)\s*$", prefix):
                bounds["min_words"] = count
            else:
                bounds = {"min_words": count, "max_words": count}
        if bounds:
            result.pop("min_words", None)
            result.pop("max_words", None)
            result.update(bounds)
        for match in re.finditer(
            r"\b(\d+|one|two|three|four|five|un|une|deux|trois|quatre|cinq)\s+"
            r"(?:(?:official|distinct|different|officiels?|officielles?|distinctes?)\s+)*"
            r"(?:liens?|links?|citations?|sources?)\b",
            lower,
        ):
            if _writing_count_is_directive(lower, match.start()):
                number = match[1]
                result["min_citations"] = int(number) if number.isdigit() else numbers[number]
        domains, excluded = _writing_source_domains(lower)
        if domains or excluded:
            result["required_source_domains"] = [
                domain
                for domain in (domains or result.get("required_source_domains", []))
                if domain not in excluded
            ]
    return WritingRequirements.model_validate(result).model_dump(exclude_unset=True)


class WritingRequirementsError(ValueError):
    reason_code = "writing_requirements_unmet"

    def __init__(self, violations: list[str]) -> None:
        self.violations = tuple(violations)
        super().__init__(self.reason_code)


def writing_word_count(text: str) -> int:
    """Count Unicode prose tokens, excluding source-ID appendix, URLs and markers."""
    text = re.sub(r"(?m)^\s*\[S\d+\]\s*<https?://[^>]+>\s*$", "", text)
    text = re.sub(r"https?://[^\s<>\"`]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[S\d+\]", "", text)
    return len(re.findall(r"[^\W_]+(?:['’−-][^\W_]+)*", text, flags=re.UNICODE))


def validate_writing_requirements(text: str, requirements: object, allowed_urls: set[str]) -> None:
    """Check length, distinct supplied citations and domain coverage, not semantics."""
    spec = WritingRequirements.model_validate(requirements)
    failures: list[str] = []
    count = writing_word_count(text)
    if spec.min_words is not None and count < spec.min_words:
        failures.append("min_words")
    if spec.max_words is not None and count > spec.max_words:
        failures.append("max_words")
    cited = cited_research_urls(text, allowed_urls)
    if spec.min_citations is not None and len(cited) < spec.min_citations:
        failures.append("min_citations")
    hosts = {(urlsplit(url).hostname or "").lower() for url in cited}
    if any(
        not any(host == domain or host.endswith("." + domain) for host in hosts)
        for domain in spec.required_source_domains
    ):
        failures.append("required_source_domains")
    if failures:
        raise WritingRequirementsError(failures)


class WritingFailureDiagnostics(_StrictModel):
    """Bounded worker measurements, never a delivered draft or semantic proof."""

    schema_version: Literal["1.0"]
    kind: Literal["writing_requirement_diagnostics"]
    reason: Literal["writing_requirements_unmet"]
    word_count: int = Field(ge=0, le=24_000)
    min_words: int | None = Field(ge=1, le=100_000)
    max_words: int | None = Field(ge=1, le=100_000)
    citation_count: int = Field(ge=0, le=MAX_RESEARCH_SOURCES)
    min_citations: int | None = Field(ge=0, le=5)
    required_source_domains: list[str] = Field(max_length=5)
    cited_source_domains: list[str] = Field(max_length=MAX_RESEARCH_SOURCES)
    failures: list[
        Literal["min_words", "max_words", "min_citations", "required_source_domains"]
    ] = Field(min_length=1, max_length=4)


class WritingPreviousAttemptFeedback(_StrictModel):
    """An explicitly linked failed attempt; measurements are not accepted evidence."""

    node_id: str = Field(pattern=r"^node_[A-Za-z0-9_-]+$", max_length=128)
    worker_job_id: str = Field(pattern=r"^job_[A-Za-z0-9._:-]+$", max_length=200)
    diagnostics: WritingFailureDiagnostics


class WritingPayload(_StrictModel):
    schema_version: Literal["1.0"]
    objective: str = Field(min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS)
    step_objective: str | None = Field(
        default=None, min_length=1, max_length=MAX_WRITING_CONTEXT_CHARACTERS
    )
    conversation: list[WritingConversationMessage] = Field(
        max_length=MAX_WRITING_CONVERSATION_MESSAGES
    )
    research_sources: list[WritingResearchSource] = Field(
        default_factory=list, max_length=MAX_RESEARCH_SOURCES
    )
    requirements: WritingRequirements | None = None
    durable_context: dict[str, Any] | None = None
    previous_attempt_feedback: WritingPreviousAttemptFeedback | None = None

    @field_validator("durable_context")
    @classmethod
    def validate_durable_context(cls, value: object) -> dict[str, Any]:
        return validate_agent_capsule(value)

    @field_validator("previous_attempt_feedback")
    @classmethod
    def validate_previous_attempt(
        cls, value: WritingPreviousAttemptFeedback | None
    ) -> WritingPreviousAttemptFeedback:
        if value is None:
            raise ValueError("previous attempt feedback must be an object when provided")
        return value

    @field_validator("requirements")
    @classmethod
    def validate_requirements(cls, value: WritingRequirements | None) -> WritingRequirements:
        if value is None:
            raise ValueError("requirements must be an object when provided")
        return value

    dependency_context: list[DependencyContextItem] = Field(
        default_factory=list, max_length=MAX_DEPENDENCY_ITEMS
    )

    @field_validator("dependency_context")
    @classmethod
    def validate_dependency_bytes(
        cls, value: list[DependencyContextItem]
    ) -> list[DependencyContextItem]:
        if (
            len(
                json.dumps(
                    [item.model_dump() for item in value], ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            > MAX_DEPENDENCY_BYTES
        ):
            raise ValueError("dependency context exceeds its byte limit")
        return value

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        return _checked_text(value)

    @field_validator("step_objective")
    @classmethod
    def validate_step_objective(cls, value: str | None) -> str:
        # Optional means absent on the wire, preserving the older payload shape.
        if value is None:
            raise ValueError("step objective must be text when provided")
        return _checked_text(value)

    @model_validator(mode="after")
    def validate_payload_size(self) -> WritingPayload:
        if self.previous_attempt_feedback is not None:
            _validate_diagnostics_for_task(self.previous_attempt_feedback.diagnostics, self)
        serialized_sources = [source.model_dump() for source in self.research_sources]
        count_limit, source_limit = research_source_limits(serialized_sources)
        if len(serialized_sources) > count_limit:
            raise ValueError("writing research source count exceeds its limit")
        if (
            len(
                json.dumps(
                    [s.model_dump() for s in self.research_sources],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > source_limit
        ):
            raise ValueError("research sources exceed their UTF-8 byte limit")
        encoded = json.dumps(
            self.model_dump(exclude_unset=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_WRITING_PAYLOAD_BYTES:
            raise ValueError("writing payload exceeds its UTF-8 byte limit")
        return self


class WritingResult(_StrictModel):
    schema_version: Literal["1.0"]
    content_trust: Literal["untrusted"]
    text: str = Field(min_length=1, max_length=MAX_WRITING_TEXT_BYTES)
    summary: str = Field(min_length=1, max_length=MAX_WRITING_SUMMARY_CHARACTERS)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _checked_text(value, max_bytes=MAX_WRITING_TEXT_BYTES)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _checked_text(value)


class WritingDeclinedResult(WritingResult):
    """A model-declared refusal, never a delivered writing draft."""

    outcome: Literal["declined"]
    model_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class WritingNonDeliveryResult(WritingResult):
    outcome: Literal["declined", "needs_clarification", "insufficient_sources"]
    model_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    question: str | None = Field(default=None, min_length=12, max_length=800)

    @model_validator(mode="after")
    def validate_question(self) -> WritingNonDeliveryResult:
        if self.outcome == "needs_clarification":
            if self.question is None or not meaningful_writing_question(self.question):
                raise ValueError("clarification requires a specific bounded question")
        elif "question" in self.model_fields_set:
            raise ValueError("only a clarification may contain a question")
        return self


def meaningful_writing_question(value: str) -> bool:
    _checked_text(value)
    # This rejects known generic requests, not all semantically vague questions.
    lowered = value.strip().casefold().replace("’", "'")
    lowered = re.sub(r"\b(?:please|s'il vous pla[îi]t)\b", "", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip(" ,?.!")
    generic = (
        re.fullmatch(
            r"(?:(?:can|could|would) you )?(?:provide|give|share) "
            r"(?:more|additional|further) (?:details|information|context)"
            r"(?: or clarify your request)?"
            r"|(?:pouvez|pourriez)-vous (?:préciser votre demande|fournir "
            r"(?:plus d'informations|(?:le|un) plan(?: détaillé)?(?: que vous souhaitez)?))",
            lowered,
        )
        is not None
    )
    vague = {
        "clarify",
        "clarify your request",
        "provide the draft",
        "provide the plan",
        "please clarify",
        "please clarify your request",
        "could you clarify your request",
        "could you please clarify your request",
        "can you provide more information",
        "could you please provide more information or clarify your request",
        "pouvez-vous préciser",
        "pouvez-vous préciser votre demande",
        "merci de préciser",
        "pouvez-vous fournir plus d'informations",
        "pouvez-vous fournir le plan",
        "please provide the draft",
        "please provide the plan",
    }
    return (
        12 <= len(value) <= 800
        and len(value.split()) >= 4
        and "?" in value
        and lowered not in vague
        and not generic
    )


class UnsupportedCitationError(ValueError):
    def __init__(self) -> None:
        super().__init__("unsupported_citation")


def _citation_tokens(text: str, allowed_urls: set[str]) -> list[str]:
    """Check bounded HTTP(S) tokens exactly; never normalize a destination."""
    tokens: list[str] = []
    for match in re.finditer(r"https?://(?:(?!\]\()[^\s<>\"`])+", text, flags=re.IGNORECASE):
        token = match.group()
        if match.start() and text[match.start() - 1] == "'":
            # An apostrophe inside a URL is otherwise a real path/query byte.
            quoted_end = re.search(r"'[.,;:!]*$", token)
            if quoted_end is not None:
                token = token[: quoted_end.start()]
        if token in allowed_urls:
            tokens.append(token)
            continue
        # Strip only unmatched surrounding closing delimiters, with sentence
        # punctuation outside them. Balanced URL parentheses remain part of it.
        for _ in range(8):  # Bound work even for adversarial delimiter runs.
            closing = re.search(r"([)\]}])([.,;:!]*)$", token)
            if closing is None:
                break
            end = closing.group(1)
            opening = {")": "(", "]": "[", "}": "{"}[end]
            prefix = token[: closing.start() + 1]
            if prefix.count(end) <= prefix.count(opening):
                break
            token = token[: closing.start()]
        if token in allowed_urls:
            tokens.append(token)
            continue
        # Query/fragment punctuation is ambiguous: require its exact bytes.
        # Markdown/autolinks still delimit those URLs without rewriting them.
        if "?" not in token and "#" not in token:
            token = token.rstrip(".,;:!")
        tokens.append(token)
    return tokens


def cited_research_urls(text: str, allowed_urls: set[str]) -> set[str]:
    return {token for token in _citation_tokens(text, allowed_urls) if token in allowed_urls}


def unsupported_citation(text: str, allowed_urls: set[str]) -> bool:
    return any(token not in allowed_urls for token in _citation_tokens(text, allowed_urls))


def validate_writing_result(value: object, *, payload: object = None) -> dict[str, Any]:
    """Validate the full draft without treating it as proof of external actions."""
    result = WritingResult.model_validate(value).model_dump()
    if payload is not None:
        task = WritingPayload.model_validate(payload)
        allowed = {source.url for source in task.research_sources}
        if allowed and any(
            unsupported_citation(result[key], allowed) for key in ("text", "summary")
        ):
            raise UnsupportedCitationError()
        requirements = (
            task.requirements.model_dump(exclude_unset=True)
            if task.requirements is not None
            else derive_writing_requirements(task.objective, durable_writing_conversation(task))
        )
        validate_writing_requirements(result["text"], requirements, allowed)
    return result


def validate_writing_failure_diagnostics(value: object, *, payload: object) -> dict[str, Any]:
    """Bind every reported threshold/host to admitted inputs; accept no free text.

    Counts are worker observations of rejected output. They aid a budgeted next
    attempt, but cannot establish correctness, source support, or completion.
    """
    report = WritingFailureDiagnostics.model_validate(value)
    task = WritingPayload.model_validate(payload)
    return _validate_diagnostics_for_task(report, task)


def _validate_diagnostics_for_task(
    report: WritingFailureDiagnostics, task: WritingPayload
) -> dict[str, Any]:
    requirements = task.requirements or WritingRequirements.model_validate(
        derive_writing_requirements(task.objective, durable_writing_conversation(task))
    )
    for key in ("min_words", "max_words", "min_citations", "required_source_domains"):
        if getattr(report, key) != getattr(requirements, key):
            raise ValueError("writing diagnostics do not match admitted requirements")
    urls = {source.url for source in task.research_sources}
    hosts = {(urlsplit(url).hostname or "").lower() for url in urls}
    cited = report.cited_source_domains
    if cited != sorted(set(cited)) or not set(cited).issubset(hosts):
        raise ValueError("writing diagnostic domains are not admitted")
    available_cited_urls = sum((urlsplit(url).hostname or "").lower() in cited for url in urls)
    if (
        bool(cited) != bool(report.citation_count)
        or not len(cited) <= report.citation_count <= available_cited_urls
    ):
        raise ValueError("writing diagnostic citation count is inconsistent")
    failures: list[str] = []
    if report.min_words is not None and report.word_count < report.min_words:
        failures.append("min_words")
    if report.max_words is not None and report.word_count > report.max_words:
        failures.append("max_words")
    if report.min_citations is not None and report.citation_count < report.min_citations:
        failures.append("min_citations")
    if any(
        not any(host == domain or host.endswith("." + domain) for host in cited)
        for domain in report.required_source_domains
    ):
        failures.append("required_source_domains")
    if report.failures != failures:
        raise ValueError("writing diagnostic failures do not match measurements")
    return report.model_dump()


def durable_writing_conversation(task: WritingPayload) -> list[dict[str, str]]:
    """Chronological source-backed user requirements precede current user updates."""
    durable = task.durable_context or {}
    return [
        {"role": "user", "content": item["text"]} for item in durable.get("requirements", [])
    ] + [message.model_dump() for message in task.conversation]


def writing_failure_summary(validated: dict[str, Any]) -> str:
    """Fixed-format error that fits the node's 500-character evaluator boundary."""
    parts = ["writing_requirements_unmet: worker_observation"]
    for key in ("word_count", "min_words", "max_words", "citation_count", "min_citations"):
        value = validated[key]
        if value is not None:
            parts.append(f"{key}={value}")
    parts.append("failures=" + ",".join(validated["failures"]))
    return "; ".join(parts)


def validate_writing_non_delivery_result(
    value: object, *, payload: object = None
) -> dict[str, Any]:
    """Keep an explicit non-delivery separate from text proving completion."""
    result = WritingNonDeliveryResult.model_validate(value).model_dump(exclude_unset=True)
    if payload is not None:
        task = WritingPayload.model_validate(payload)
        allowed = {source.url for source in task.research_sources}
        fields = ("text", "summary", "question")
        if allowed and any(unsupported_citation(result[k], allowed) for k in fields if k in result):
            raise UnsupportedCitationError()
    return result


def validate_writing_declined_result(value: object, *, payload: object = None) -> dict[str, Any]:
    """Compatibility helper for the original refusal-only contract."""
    WritingDeclinedResult.model_validate(value)
    return validate_writing_non_delivery_result(value, payload=payload)
