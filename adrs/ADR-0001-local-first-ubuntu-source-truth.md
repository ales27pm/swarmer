# ADR-0001 — Local-first iPhone, Ubuntu Source of Truth

## Status

Accepted.

## Context

The app must feel local on iPhone, but all agents need shared state and long-term memory.

## Decision

Use local SQLite on iPhone as a replica/cache and Ubuntu as source of truth.

## Consequences

Positive:

- fast mobile UX;
- offline outbox;
- consistent multi-agent state;
- easier backups.

Negative:

- sync complexity;
- conflict handling required.
