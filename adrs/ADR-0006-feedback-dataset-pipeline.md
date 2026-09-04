# ADR-0006 — Feedback Dataset Pipeline, No Live Auto-training

## Status

Accepted.

## Context

The system should improve over time and accumulate examples for evaluation/training.

## Decision

Collect feedback and export datasets, but require manual review, redaction, versioning, eval and rollback before training/deployment.

## Consequences

Positive:

- safer improvement;
- useful evals quickly;
- future LoRA path.

Negative:

- manual review effort;
- slower than reckless auto-training.
