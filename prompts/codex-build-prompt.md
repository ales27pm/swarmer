# Codex Build Prompt

Use this prompt to start implementation in Codex.

You are building `monGARS Swarm App`.

Read these files first:

1. `MASTER_SPEC.md`
2. `docs/03-system-architecture.md`
3. `docs/04-mobile-expo-architecture.md`
4. `docs/05-ubuntu-control-plane.md`
5. `docs/08-state-memory-sync.md`
6. `docs/09-permission-gateway.md`
7. `docs/15-testing-strategy.md`
8. `docs/23-implementation-plan.md`
9. `docs/25-codex-handoff.md`

Implement the first vertical slice only:

- repo skeleton;
- shared schemas;
- FastAPI health/pairing/state/sync;
- Expo Router shell with Chat/Tasks/Approvals/Memory/Agents/Settings and task detail;
- SQLite local replica;
- SecureStore token storage;
- explicit REST bootstrap and refresh;
- create task flow;
- configured local OpenAI-compatible orchestrator proposal;
- gateway approval flow;
- harmless executor writing test artifact after approval;
- audit event;
- feedback event.

Tests may replace external boundaries with deterministic fixtures, but runtime
code must not fabricate model replies, executor evidence, or completion. Mobile
WebSocket subscription/reconnect and offline outbox remain roadmap.

Constraints:

- no direct LLM execution path;
- no direct DB writes by models or agents; control-plane services own transactional mutations;
- no direct iPhone data access outside Capability Broker;
- all model/tool outputs must validate against schema;
- all tests local; no GitHub Actions required.

After coding, run local checks and report exact failures if any.
