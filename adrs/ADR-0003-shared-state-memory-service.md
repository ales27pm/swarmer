# ADR-0003 — Shared State and Memory Services

## Status

Accepted.

## Context

Agents need common state and long-term context, but direct DB writes by agents are risky.

## Decision

All writes go through State Service or Memory Service.

## Consequences

Positive:

- consistency;
- auditability;
- easier sync;
- safer memory governance.

Negative:

- more service code;
- agent tools need strict contracts.
