# 25 — Codex / Builder Handoff

## Mission

Maintain the authenticated `0.6` vertical slice, using OpenAPI and
`docs/21-acceptance-criteria.md` as current truth. The remaining architecture in
this document is roadmap, not a claim that the runtime already implements it.

## Non-negotiable architecture

- Mobile app: Expo + React Native + TypeScript.
- Server: Ubuntu local FastAPI control plane.
- Ubuntu is the source of truth for state/memory/audit.
- iPhone has a bootstrap-hydrated SQLite cache; UI reads remain authenticated REST.
- LLMs never execute directly.
- Permission Gateway validates all sensitive actions.
- Agents and models never write SQLite directly; control-plane services own mutations.
- A durable agent message board is roadmap.
- Current lexical memory is server-authoritative; a dedicated vector Memory Service is roadmap.
- iPhone native data and its Phone Capability Broker are roadmap.
- Feedback is stored now; eval/dataset export is roadmap.

## Current slice

1. Pair over loopback or HTTPS and retain an origin-bound device token.
2. Create tasks and ask the configured local OpenAI-compatible model for a
   schema-validated proposal.
3. Keep proposal-only text distinct from verified executor completion.
4. Bind sensitive tool calls to one-use approvals with trusted provenance.
5. Execute only supported tools and retain audit/evidence.
6. Hydrate the SQLite cache through bootstrap; use explicit REST refresh in UI.

## Roadmap after the current slice

- mobile WebSocket subscription/reconnect and live cache reconciliation;
- offline outbox and pull/push sync;
- durable Redis/NATS message board;
- vector embeddings and retrieval injection;
- native iPhone capability broker;
- feedback dataset export.

Tests may use deterministic fixtures, but product code must not simulate model or
executor completion.

## Code style

- Prefer small modules.
- Keep DB writes inside control-plane services and transactions; never expose DB access to agents.
- No direct native access outside Phone Capability Broker.
- No direct shell/file/network execution outside Gateway + Executor.
- Use TypeScript types and Python Pydantic models from schemas.
- Keep tests runnable without live LLM.

## Local check commands

Mobile:

```bash
npm run typecheck
npm run lint
npm test -- --runInBand
npx expo-doctor
```

Backend:

```bash
ruff format --check .
ruff check .
mypy services
pytest -q
bandit -r services
```

## Stop condition

Do not add unrelated features until the vertical slice passes.
