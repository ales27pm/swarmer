"""Conservative lexical admission for shared facts, without changing user rules."""

from __future__ import annotations

import re

# Function words cannot establish a fact's topic. This is deliberately a local,
# deterministic gate; it does not translate, infer policy, or grant authority.
_FUNCTION_WORD_TEXT = """a an and are as at be been being but by can could did do does for from had
has have how i if in into is it its may of on or our should that the their
them there these they this those to was we were what when where which who
will with would you your
au aux avec ce ces cet cette dans de des du elle elles en est et eux il ils
je la le les leur leurs lui ma mais mes moi mon ne nos notre nous on ou par
pas pour qu que quel quelle qui sa sans se ses si son sont sous sur ta te
tes toi ton tu un une vos votre vous être été"""
_FUNCTION_WORDS = frozenset(_FUNCTION_WORD_TEXT.split())
_MAX_PREFILTER_CHARS = 32_000


def memory_relevance_terms(text: str) -> frozenset[str]:
    # Redaction placeholders shared by unrelated sources are not evidence.
    text = re.sub(r"<(?:redacted-secret|protected-path)>", " ", text.casefold())
    return frozenset(re.findall(r"[^\W_]{2,}", text)) - _FUNCTION_WORDS


def general_fact_may_be_relevant(
    *, scope: str, kind: str, source: str, query_terms: frozenset[str]
) -> bool:
    """Cheap candidate check only; callers must still validate the redacted text."""
    if scope not in {"general", "global"} or kind.strip().casefold() != "fact":
        return True
    # The API bounds raw memory content to 32k characters. Legacy larger rows
    # stay candidates: redaction may expose a relevant term after a long secret.
    if len(source) > _MAX_PREFILTER_CHARS:
        return True
    text = source[:_MAX_PREFILTER_CHARS].casefold()
    return any(term in text for term in query_terms)


def general_fact_is_relevant(
    *, scope: str, kind: str, source: str, query_terms: frozenset[str]
) -> bool:
    """Filter only shared facts; preserve all existing instruction/scope behavior.

    Pinning affects priority, not factual relevance. A preference or constraint
    must not disappear merely because its vocabulary differs from the task.
    """
    if scope not in {"general", "global"} or kind.strip().casefold() != "fact":
        return True
    return bool(query_terms & memory_relevance_terms(source))
