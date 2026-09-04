# 16 — Code Analysis and Quality Gates

## Objectif

Avoir des checks locaux sérieux sans dépendre de GitHub Actions.

## Mobile gates

### TypeScript

```bash
npx tsc --noEmit
```

### ESLint

```bash
npm run lint
```

### Format

```bash
npm run format:check
```

### Tests

```bash
npm test -- --runInBand
```

### Expo health

```bash
npx expo-doctor
```

### Dependency sanity

```bash
npm outdated
npm audit --omit=dev
```

## Backend gates

### Python format/lint

```bash
ruff format --check .
ruff check .
```

### Types

```bash
mypy services
```

### Tests

```bash
pytest -q
```

### Security scan

```bash
bandit -r services
```

### Dependency audit

```bash
pip-audit
```

## Schema gates

- Validate JSON Schemas.
- Validate OpenAPI.
- Validate config YAML.
- Validate model manifest.
- Validate permissions rules.

## Prompt gates

Prompts doivent être testés avec fixtures:

- pas de prose libre pour tool call;
- demande permission au bon moment;
- ne révèle pas secrets;
- respecte output schema;
- ne modifie pas scope;
- refusal normalized.

## LLM output gates

Chaque sortie modèle passe par:

1. JSON parse.
2. Schema validation.
3. Tool name allowlist.
4. Argument validation.
5. Permission evaluation.
6. Audit pre-log.
7. Execute ou ask.

## Code review checklist

- Le code ajoute-t-il un chemin d'action sans gateway?
- Le code expose-t-il un secret au prompt?
- Le code écrit-il directement dans DB hors State Service?
- Le code lit-il des données iPhone sans capability request?
- Le code ignore-t-il sync conflicts?
- Le code gère-t-il offline/reconnect?
- Le code est-il testable sans LLM live?

## Local `check` script recommandé

```bash
#!/usr/bin/env bash
set -euo pipefail

pushd mobile
npm run typecheck
npm run lint
npm test -- --runInBand
npx expo-doctor
popd

pushd server
ruff format --check .
ruff check .
mypy services
pytest -q
bandit -r services
popd

python scripts/validate_schemas.py
python scripts/validate_openapi.py
```

## Definition of clean code for this project

- Typed at boundaries.
- Small tools.
- No god service.
- No model-driven direct execution.
- Explicit permission path.
- Every event has trace id.
- Every external/native action has test fixture.
