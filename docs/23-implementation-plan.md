# 23 — Implementation Plan

## Build order exact

### Step 1 — Repo skeleton

Créer:

```text
mobile/
server/
workers/
packages/schemas/
configs/
prompts/
scripts/
docs/
```

Copier ce docs pack dans `docs/`.

### Step 2 — Shared schemas

Implémenter les JSON Schemas dans `packages/schemas`.

Priorité:

1. event envelope;
2. tool call;
3. permission request;
4. agent card;
5. sync operation;
6. feedback event.

### Step 3 — Backend base

- FastAPI app.
- Settings loader.
- SQLite schema.
- State Service.
- Health endpoint.
- Pairing endpoint.

### Step 4 — Mobile base

- Expo app.
- Routes.
- API client.
- SQLite wrapper.
- SecureStore.
- Settings screen.
- Pairing flow.

### Step 5 — Sync

- Bootstrap.
- Pull.
- Push.
- WebSocket.
- Outbox.
- Reconnect handling.

### Step 6 — Task flow

- Create task endpoint.
- Task state table.
- Task UI.
- Server event push.

### Step 7 — Orchestrator shell

- LLM client.
- Prompt loader.
- Model manifest.
- JSON parser.
- Tool validation.
- No-op tools first.

### Step 8 — Gateway

- Permission rules YAML.
- Risk engine.
- Approval creation.
- Approval UI.
- Decision endpoint.

### Step 9 — First executor

- File read safe.
- File diff proposal.
- File write after approval.
- Test command after approval.

### Step 10 — Message board

- Redis Streams.
- Event bus abstraction.
- Worker consumer.
- Agent heartbeat.

### Step 11 — Memory

- Embedding model.
- Chunker.
- FAISS.
- Memory search API.
- Memory UI.

### Step 12 — iPhone bridge

- Location.
- Contacts.
- Calendar.
- Photos picker.
- Broker request/response.

### Step 13 — Feedback/evals

- Feedback UI.
- Feedback event store.
- Eval export.
- Parser regression fixtures.

### Step 14 — Custom native / on-device LLM

- Add dev client.
- Add local native module.
- MLX/Core ML prototype.
- Benchmark.

## Implementation notes

- Ne pas commencer par le on-device LLM. Commencer par la boucle app ↔ Ubuntu ↔ task ↔ approval.
- Les modèles peuvent être branchés après les schémas/gateway.
- Les agents doivent être testables avec fake model output.
- Les iPhone capabilities viennent après le broker et les approvals.

## First vertical slice

La première tranche utile:

> Depuis iPhone, demander “liste les fichiers du projet X”; Ubuntu crée tâche; worker lit seulement le dossier autorisé; retourne résultat; feedback enregistré.

Deuxième tranche:

> Demander “modifie tel fichier”; worker propose diff; gateway demande permission; iPhone approuve; patch appliqué; tests lancés; audit enregistré.
