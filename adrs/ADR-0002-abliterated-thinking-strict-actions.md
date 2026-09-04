# ADR-0002 — Abliterated Where It Thinks, Strict Where It Acts

## Status

Accepted.

## Context

The user wants models that do not constantly refuse or moralize, while still keeping real-world actions under control.

## Decision

Use abliterated models for orchestrator/workers where useful. Keep permissions, validation, sandbox and audit deterministic outside the model.

## Consequences

Positive:

- less model-level friction;
- more direct task handling;
- consistent permission UX.

Negative:

- gateway must be strong;
- model output must be treated as untrusted;
- more schema/parser tests required.
