# Testing and Code Analysis Prompt

Audit the monGARS Swarm App implementation.

Check these invariants:

1. No worker writes directly to the DB.
2. No LLM/model response executes without schema validation.
3. No file/shell/network/iPhone sensitive action bypasses Permission Gateway.
4. iPhone local SQLite sync handles offline outbox and server reconciliation.
5. Approval UI shows agent, action, target, risk, reason and exact scope.
6. Protected paths such as `.env`, keys and tokens are blocked.
7. Tests do not require live LLM for parser/gateway/sync behavior.
8. Feedback events are emitted for success, failure, invalid JSON and permission decisions.
9. Audit events include trace id, actor, action, decision and timestamp.
10. Expo native modules are installed with compatible Expo tooling.

Run:

```bash
# mobile
npm run typecheck
npm run lint
npm test -- --runInBand
npx expo-doctor

# server
ruff format --check .
ruff check .
mypy services
pytest -q
bandit -r services

# schemas
python scripts/validate_schemas.py
python scripts/validate_openapi.py
```

Report:

- pass/fail summary;
- exact failing files/commands;
- security bypasses;
- missing tests;
- minimal patches.
