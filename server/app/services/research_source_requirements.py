"""Bind explicit user page-reading directives to admitted research operations.

This deliberately recognizes a bounded FR/EN subset, not arbitrary intent. Model
prose, retrieved memory, quoted examples and external text cannot supply it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from app.services.feedback_dataset import redact_dataset_text
from app.services.research_contracts import ResearchCollectPayload, valid_research_collect_receipt
from app.services.swarm_contracts import SwarmPlanProposal

_URL = re.compile(r'https?://[^\s<>"`]+', re.IGNORECASE)
_QUOTED = re.compile(r'```[\s\S]*?```|`[^`]*`|«[^»]*»|“[^”]*”|"[^"\n]*"|(?m:^\s*>[^\n]*)')
_READ = re.compile(
    r"\b(?:read|fetch|consult|open|lis|lisez|lire|consulte\w*|ouvre\w*)\b", re.IGNORECASE
)
_NEGATIVE = re.compile(
    r"\b(?:not|never|jamais|don['’]t|avoid|exclude|excluding|without|except|"
    r"sans|sauf|exclu\w*|évite\w*|n['’]\w+\s+pas|ne\s+\w+\s+pas)\b",
    re.IGNORECASE,
)
_OBSERVATION = re.compile(
    r"\b(?:said|says|asked|requested|previously|example|exemple|disait|demandait|a dit)\b",
    re.IGNORECASE,
)


class ResearchSourceRequirementError(ValueError):
    diagnostic_code = "research_source_requirements"


def _url_text(raw: str) -> str:
    url = raw.rstrip(".,;")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def explicit_read_urls(objective: str, conversation: Sequence[dict[str, Any]]) -> list[str]:
    """Return canonical URLs explicitly requested for reading, in source order."""
    required: list[str] = []
    texts = [objective, *(m["content"] for m in conversation if m.get("role") == "user")]
    for raw in texts:
        text = _QUOTED.sub(lambda m: " " * len(m[0]), raw)
        # Preserve sentence punctuation outside the URL, so "Read A. B is an
        # example" cannot accidentally make B another requested page.
        masked = _URL.sub(lambda m: " " * len(_url_text(m[0])) + m[0][len(_url_text(m[0])) :], text)
        for match in _URL.finditer(text):
            prefix = re.split(r"[;\n]|[.!?](?:\s+|$)", masked[: match.start()])[-1]
            prefix = re.split(r"\b(?:but|mais)\b", prefix, flags=re.IGNORECASE)[-1]
            if _OBSERVATION.search(prefix):
                continue
            url = _url_text(match[0])
            if _NEGATIVE.search(prefix) or re.search(r"\bne\s+\w+\s+plus\b", prefix, re.IGNORECASE):
                if url in required:
                    required.remove(url)
                continue
            if not _READ.search(prefix):
                continue
            if redact_dataset_text(url) != url:
                raise ResearchSourceRequirementError("explicit reading URL contains sensitive data")
            try:
                ResearchCollectPayload.checked_source_urls([url])
            except ValueError as exc:
                raise ResearchSourceRequirementError(
                    "explicit reading URL is not admissible"
                ) from exc
            if url not in required:
                required.append(url)
    if len(required) > 6:
        raise ResearchSourceRequirementError("explicit reading sources exceed the collection bound")
    return required


def bind_research_sources(
    proposal: SwarmPlanProposal,
    objective: str,
    conversation: Sequence[dict[str, Any]],
    *,
    already_read: Mapping[str, str] | None = None,
) -> SwarmPlanProposal:
    """Preserve explicit URLs before hashing/persisting the effective plan.

    A single collection can inherit missing source arguments unambiguously.
    Multiple collections must already assign each required URL, so the server
    does not guess which independent task should receive which source.
    Existing per-operation budgets and the public payload validation still apply.
    """
    urls = explicit_read_urls(objective, conversation)
    if already_read:
        fresh_requests: set[str] = set()
        for message in conversation:
            for url in explicit_read_urls("", [message]):
                if url not in already_read:
                    continue
                requested = _timestamp(message.get("created_at"))
                read_started = _timestamp(already_read[url])
                # Missing/ambiguous ordering never proves a new request was met.
                if requested is None or read_started is None or requested >= read_started:
                    fresh_requests.add(url)
        urls = [url for url in urls if url not in already_read or url in fresh_requests]
    if not urls:
        return proposal
    readers = [n for n in proposal.nodes if n.required_skill == "research.collect"]
    if not readers:
        raise ResearchSourceRequirementError("explicit page reading requires a collection node")
    if len(readers) > 1:
        assigned = {
            url for node in readers for url in (node.worker_arguments or {}).get("source_urls", [])
        }
        if not set(urls).issubset(assigned):
            raise ResearchSourceRequirementError("explicit pages are unassigned across collections")
        return proposal
    reader = readers[0]
    payload = ResearchCollectPayload.model_validate(reader.worker_arguments)
    combined = list(dict.fromkeys([*urls, *payload.source_urls]))
    if len(combined) > payload.max_pages:
        raise ResearchSourceRequirementError("explicit pages exceed the proposed page budget")
    domains = list(
        dict.fromkeys(
            [
                *payload.required_domains,
                *((urlsplit(url).hostname or "") for url in urls),
            ]
        )
    )
    arguments = {
        **(reader.worker_arguments or {}),
        "source_urls": combined,
        "required_domains": domains,
    }
    try:
        ResearchCollectPayload.model_validate(arguments)
    except ValueError as exc:
        raise ResearchSourceRequirementError(
            "explicit source binding exceeds the payload contract"
        ) from exc
    nodes = [
        node.model_copy(update={"worker_arguments": arguments}) if node is reader else node
        for node in proposal.nodes
    ]
    return proposal.model_copy(update={"nodes": nodes})


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


async def completed_read_urls(db_path: Path, goal_id: str) -> dict[str, str]:
    """Existing same-goal fetch receipts satisfy old, not newly repeated, reads."""
    urls: dict[str, str] = {}
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA query_only=ON")
        async with db.execute(
            """SELECT j.payload_json,j.result_json,j.created_at FROM plan_nodes n
            JOIN agent_jobs j ON j.id=n.worker_job_id AND j.task_id=n.task_id
              AND j.required_skill=n.required_skill
            JOIN tasks t ON t.id=j.task_id AND t.source=?
            WHERE n.goal_run_id=? AND n.required_skill='research.collect'
              AND n.status='completed' AND j.status='completed'
              AND length(CAST(j.result_json AS BLOB))<=524288""",
            (f"goal:{goal_id}", goal_id),
        ) as cursor:
            async for payload_json, result_json, created_at in cursor:
                try:
                    payload, result = json.loads(payload_json), json.loads(result_json)
                except (TypeError, ValueError):
                    continue
                started = _timestamp(created_at)
                if started is not None and valid_research_collect_receipt(result, payload):
                    for page in result["pages"]:
                        if page["status"] != "read":
                            continue
                        url = page["requested_url"]
                        previous = _timestamp(urls.get(url))
                        if previous is None or started > previous:
                            urls[url] = created_at
    return urls
