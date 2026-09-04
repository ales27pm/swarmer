# ADR-0004 — Message Board for Agent Swarm

## Status

Accepted.

## Context

Remote agents need durable communication and task status.

## Decision

Use Redis Streams for MVP, with path to NATS JetStream later.

## Consequences

Positive:

- simple local setup;
- durable queues;
- consumer groups;
- good enough for MVP.

Negative:

- cross-machine operations need careful security;
- NATS may be better later.
