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
- Expo Router shell with Chat/Tasks/Approvals/Settings;
- SQLite local replica;
- SecureStore token storage;
- WebSocket status updates;
- create task flow;
- mock orchestrator plan;
- gateway approval flow;
- harmless executor writing test artifact after approval;
- audit event;
- feedback event.

Constraints:

- no direct LLM execution path;
- no direct DB writes outside State Service;
- no direct iPhone data access outside Capability Broker;
- all model/tool outputs must validate against schema;
- all tests local; no GitHub Actions required.

After coding, run local checks and report exact failures if any.
