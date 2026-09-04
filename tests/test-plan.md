# Test Plan

## Scope

Couvre mobile Expo, backend Ubuntu, agents, permissions, sync, memory et feedback.

## Priorités

P0:

- Pairing.
- Create task.
- WebSocket status.
- Permission approval.
- Gateway blocks unsafe action.
- State sync.

P1:

- Memory search.
- iPhone location/calendar/contact capability.
- Feedback export.
- Worker heartbeat.

P2:

- On-device LLM.
- Multi-machine agents.
- NATS/Qdrant migration.

## Entry criteria

- Repo builds.
- Configs load.
- Schemas validate.
- Mock server available.

## Exit criteria

- P0 tests pass.
- No direct execution bypass.
- Audit log present for sensitive actions.
- App handles offline/reconnect.
