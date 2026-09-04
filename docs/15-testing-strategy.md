# 15 — Testing Strategy — Expo / React Native / Backend / Swarm

## Objectif

Prouver que l'app fonctionne sans dépendre de réponses LLM parfaites.

## Principe

Tester les couches déterministes fortement:

- schemas;
- gateway;
- sync;
- state;
- message board;
- permissions;
- native bridge wrappers;
- parsers de sortie modèle.

Tester les modèles par fixtures et evals, pas seulement en live.

## Mobile test stack

- TypeScript strict.
- ESLint.
- Jest / jest-expo.
- React Native Testing Library.
- Mock Service Worker ou mocks fetch.
- Tests SQLite wrapper.
- Tests WebSocket reconnect avec mock server.
- Dev client tests pour modules natifs custom.

## Mobile unit/component tests

Couvrir:

- Chat input envoie message.
- Task list affiche status.
- Approval card affiche risque/action/cible.
- Allow once envoie bonne décision.
- Deny envoie bonne décision.
- Memory search affiche résultats.
- Offline banner apparaît quand WebSocket tombe.
- Outbox rejoue après reconnect.

## Permission tests

Scénarios:

- permission iOS refusée;
- permission iOS acceptée;
- gateway demande approval;
- approval expirée;
- résultat redacted;
- capability non disponible.

## Backend tests

- pytest pour services.
- JSON Schema validation.
- Permission risk rules.
- State Service writes.
- Memory Service chunk/search.
- Agent registry heartbeat.
- Message board event handling.
- API contract tests.
- Audit hash chain.

## Model parser tests

Fixtures:

- valid tool call;
- invalid JSON;
- refusal prose;
- hallucinated tool;
- missing permission;
- too-large response.

Expected:

- parse ok;
- retry;
- refusal normalized;
- blocked unknown tool;
- permission request generated.

## E2E MVP flows

### Flow 1 — Pairing

1. Start Ubuntu API.
2. Open iPhone app.
3. Scan/enter pairing code.
4. Token saved in SecureStore.
5. `/sync/bootstrap` succeeds.

### Flow 2 — Simple task

1. User sends “résume l'état du swarm”.
2. Backend creates task.
3. Orchestrator responds.
4. Task completed.
5. App updates live.

### Flow 3 — Permission required

1. User asks to modify a project file.
2. Code agent proposes patch.
3. Gateway creates approval.
4. App shows approval card.
5. User allows once.
6. Executor writes patch.
7. Audit event recorded.

### Flow 4 — iPhone capability

1. Agent needs current location.
2. Broker sends `iphone.capability.requested`.
3. App asks user.
4. Location permission flow runs.
5. Result returns to task.
6. Data TTL applied.

### Flow 5 — Offline sync

1. iPhone offline.
2. User creates message.
3. Message enters outbox.
4. Network returns.
5. Push sync sends op.
6. Server returns seq.
7. Local status becomes synced.

## Android emulator QA

Même si iPhone est cible principale, Android emulator sert à tester rapidement UI flows React Native:

- launch app;
- tap chat input;
- send message;
- inspect UI tree;
- screenshot failure;
- collect logcat.

Voir `tests/emulator-qa-runbook.md`.

## Test commands

Mobile:

```bash
npm run typecheck
npm run lint
npm test
npx expo-doctor
npx expo start
```

Backend:

```bash
ruff check .
mypy .
pytest
bandit -r services
```

Contracts:

```bash
python scripts/validate_schemas.py
python scripts/validate_openapi.py
```

## Release gate

Une release locale passe seulement si:

- typecheck mobile OK;
- lint mobile OK;
- tests mobile OK;
- backend tests OK;
- schema tests OK;
- permission fixtures OK;
- sync e2e OK;
- audit integrity OK.
