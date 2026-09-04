# 25 — Codex / Builder Handoff

## Mission

Build the monGARS Swarm App from this documentation pack.

## Non-negotiable architecture

- Mobile app: Expo + React Native + TypeScript.
- Server: Ubuntu local FastAPI control plane.
- Ubuntu is the source of truth for state/memory/audit.
- iPhone has SQLite local replica and outbox sync.
- LLMs never execute directly.
- Permission Gateway validates all sensitive actions.
- Agents communicate through message board.
- Memory writes go through Memory Service.
- iPhone native data goes through Phone Capability Broker.
- Feedback events must be stored for eval/dataset pipeline.

## Build priority

1. Create repo skeleton.
2. Implement shared schemas.
3. Implement FastAPI health/pairing/state/sync.
4. Implement Expo app routes and local SQLite.
5. Implement WebSocket live updates.
6. Implement task creation and task timeline.
7. Implement Permission Gateway and Approval UI.
8. Implement first files/code worker.
9. Add Redis Streams.
10. Add Memory Service with embeddings.
11. Add iPhone native capabilities.
12. Add feedback/eval export.

## First vertical slice

Create a working flow:

1. User pairs iPhone with Ubuntu.
2. User sends a message from Chat.
3. Ubuntu creates task.
4. Orchestrator returns a mock/fixture plan.
5. Task updates appear live on iPhone.
6. If plan includes file write, gateway creates approval.
7. User approves once.
8. Executor writes a harmless test artifact.
9. Audit and feedback events are recorded.

## Code style

- Prefer small modules.
- No direct DB writes outside State Service.
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
