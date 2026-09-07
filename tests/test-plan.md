# Test Plan

## Scope

Couvre mobile Expo, backend Ubuntu, agents, permissions, sync, memory et feedback.
Le slice courant couvre le bootstrap REST et le rafraîchissement manuel; les
tests mobile WebSocket/reconnexion, outbox et capacités natives restent roadmap.

## Priorités

P0:

- Pairing.
- Create task.
- Bootstrap REST et cache SQLite.
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
- Les critères locaux de `docs/21-acceptance-criteria.md` sont couverts sans
  revendiquer les parcours roadmap offline/reconnexion.
