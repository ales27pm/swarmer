"""Bounded server-owned explanations; never persist exception or model prose."""

from __future__ import annotations

# Public explanation and correction guidance are literals, not rejected response text.
DIAGNOSTICS: dict[str, tuple[str, str]] = {
    "invalid_json": (
        "Le plan n'est pas un objet JSON valide.",
        "Return exactly one complete JSON object matching the supplied schema.",
    ),
    "duplicate_json_key": (
        "Le plan contient une clé JSON répétée.",
        "Emit each JSON object key exactly once; never repeat fields.",
    ),
    "invalid_fields": (
        "Un champ du plan ne respecte pas le format attendu.",
        "Check required fields, types, enum values and bounds against the supplied schema.",
    ),
    "project_plan_shape": (
        "Un plan peut confier les modifications du projet à un seul agent, avec les dépendances nécessaires. L’ancien générateur Python ne consomme pas de dépendances.",
        "Use at most one project-mutating worker across code.build_project and code.generate_python combined; other capabilities and required dependencies are allowed. Legacy code.generate_python must have dependencies=[] and optional_dependencies=[] because its payload cannot consume worker results.",
    ),
    "unavailable_skill": (
        "Le plan demande une compétence absente des agents disponibles.",
        "Use only skills in the structured agent cards. Preserve unmet requirements.",
    ),
    "policy_denied": (
        "Une compétence demandée n'est pas autorisée.",
        "Do not bypass policy or invent permission; preserve the unmet requirement.",
    ),
    "duplicate_node": (
        "Plusieurs étapes utilisent le même identifiant.",
        "Give every proposed node a unique temporary_id.",
    ),
    "repeated_dependency": (
        "Une étape répète une dépendance.",
        "List each dependency once, as either hard or optional, never both.",
    ),
    "self_dependency": (
        "Une étape dépend d'elle-même.",
        "A node cannot include its own temporary_id in either dependency list.",
    ),
    "unknown_dependency": (
        "Le plan contient une dépendance inconnue.",
        (
            "Dependencies must name other proposed nodes' temporary_id, never goal or card IDs. "
            "Independent nodes use empty dependency lists; do not invent inputs."
        ),
    ),
    "cyclic_dependencies": (
        "Les dépendances du plan forment une boucle.",
        "Return an acyclic graph: no node may directly or indirectly depend on itself.",
    ),
    "step_budget": (
        "Le plan dépasse le nombre d'étapes autorisé.",
        "Respect the remaining step budget without dropping the requested outcome.",
    ),
    "parallelism_budget": (
        "Le plan dépasse le nombre d'agents simultanés autorisé.",
        "Set max_parallelism within the goal's stated parallelism budget.",
    ),
    "objective_mismatch": (
        "Le plan ne correspond pas au but demandé.",
        "Bind the top-level objective to the exact current goal card_id, without rewriting it.",
    ),
}


def known_diagnostic(value: object) -> str | None:
    return value if isinstance(value, str) and value in DIAGNOSTICS else None


def rejection_diagnostic(error: BaseException) -> str | None:
    """Follow explicit exception causes only; never classify arbitrary exception text."""
    for _ in range(8):
        code = known_diagnostic(getattr(error, "diagnostic_code", None))
        if code is not None:
            return code
        if error.__cause__ is None:
            break
        error = error.__cause__
    return None


def rejection_reason(default: str, code: object) -> str:
    safe = known_diagnostic(code)
    return f"{default} {DIAGNOSTICS[safe][0]}" if safe else default
