# MASTER SPEC — monGARS Swarm App

Date: 2026-09-04  
Statut: Draft build-ready

## 1. Résumé

monGARS Swarm App est une application iPhone local-first qui pilote un control plane Ubuntu hébergeant un orchestrateur LLM, une passerelle de permissions, une mémoire longue durée et un swarm d'agents autonomes. L'app iPhone sert de console, d'interface vocale, de source de données natives autorisées et de panneau d'approbation. Ubuntu garde la vérité officielle: state, mémoire, audit log, registry d'agents et bus de messages.

## 2. Architecture mentale

```text
Utilisateur
  ↓ voix/chat/tap
App iPhone Expo
  ├─ Local SQLite replica/cache
  ├─ Native Capability Bridge
  ├─ Approval UI
  └─ Secure WebSocket Client
       ↓
Ubuntu Control Plane
  ├─ API Gateway / Auth
  ├─ Orchestrator LLM abliterated
  ├─ Permission Gateway stricte
  ├─ Agent Registry
  ├─ Message Board Redis/NATS
  ├─ State Service SQLite/Postgres
  ├─ Memory Service FAISS/Qdrant
  ├─ Feedback Service
  ├─ Audit Ledger append-only
  └─ Worker Runtime(s)
       ↓
Agents distants
  ├─ Code worker
  ├─ Files worker
  ├─ Research worker
  ├─ CRM worker
  ├─ Design worker
  └─ Phone broker worker
```

## 3. Règles fondatrices

1. **Ubuntu est la source de vérité.** L'iPhone conserve une réplica locale pour fluidité, pas l'état maître.
2. **Local-first côté iPhone.** L'app lit/écrit localement, puis synchronise avec Ubuntu.
3. **Abliterated où ça pense.** Orchestrateur et workers LLM peuvent être abliterated pour éviter le moralisme inutile.
4. **Strict où ça agit.** Permission Gateway, risk engine, schémas JSON, sandbox, allowlists et audit logs restent déterministes.
5. **Aucun agent n'écrit directement dans la DB.** Les agents passent par State Service et Memory Service.
6. **Aucun agent n'accède directement au iPhone.** Les demandes passent par le Phone Capability Broker et l'app iPhone.
7. **Feedback structuré.** Chaque tâche produit des traces exploitables pour evals, dataset et fine-tuning manuel.
8. **Pas d'entraînement sauvage.** Tout dataset est revu, versionné, nettoyé et rollbackable.

## 4. Phases

### Phase 0 — Documentation et squelette

Livrables: ce paquet, repo scaffold, choix modèles, contrats API, configs initiales.

### Phase 1 — Console iPhone + Ubuntu API

- Expo app avec écran Chat, Tasks, Approvals, Memory, Settings.
- FastAPI backend local.
- Auth device pairing.
- SQLite local iPhone.
- State Service Ubuntu.
- WebSocket live updates.

### Phase 2 — Orchestrateur + Gateway

- Serveur LLM local.
- Orchestrateur abliterated Q4.
- Tool-call JSON strict.
- Permission Gateway.
- Audit ledger.
- 1 worker local: files/shell sandbox.

### Phase 3 — Swarm distribué

- Agent registry.
- Agent cards.
- Message board Redis Streams.
- Agents distants connectés au host.
- Work queue + task lifecycle.

### Phase 4 — Native iPhone Bridge

- Contacts, Calendar, Reminders, Location, Photos, Camera, Audio.
- Capabilities activées par permission explicite.
- Requests des agents vers iPhone via broker.
- Approval UI pour données sensibles.

### Phase 5 — Mémoire et feedback

- Embeddings + FAISS.
- Memory Service central.
- Feedback Service.
- Eval builder.
- Dataset export JSONL.
- LoRA candidate pipeline.

### Phase 6 — On-device LLM

- Prototype MLX/Core ML/llama.cpp mobile.
- Development build Expo.
- On-device mini-orchestrator/cache agent.
- Fallback offline.

## 5. Modèles proposés

Primary abliterated orchestrator/worker:

- `mradermacher/Hermes-3-Llama-3.2-3B-abliterated-GGUF:Q4_K_M`

Abliterated fast worker:

- `mradermacher/G9v3-3B-Heretic-Abliterated-GGUF:Q4_K_M`
- fallback equivalent: `Vortecks/G9v3-3B-Heretic-Abliterated-GGUF:Q4_K_M`

Dolphin style/fallback:

- `bartowski/Dolphin3.0-Llama3.2-3B-GGUF:Q4_K_M`

Reference non-abliterated orchestration benchmark:

- `katanemo/Plano-Orchestrator-4B`
- `mradermacher/Plano-Orchestrator-4B-GGUF`

Embeddings:

- MVP: `intfloat/multilingual-e5-small`
- Better multilingual memory: `BAAI/bge-m3`

## 6. State et mémoire

- iPhone: SQLite replica + outbox sync queue.
- Ubuntu: SQLite WAL au MVP; migration vers Postgres lorsque plusieurs writers/agents.
- Memory Service: chunking, embeddings, semantic search, metadata filters.
- Vector store: FAISS local au départ, Qdrant si besoin multi-agent/collections/filtrage.
- Event log: append-only pour replay et audit.

## 7. Message board

MVP: Redis Streams.

Streams:

- `tasks.inbox`
- `tasks.status`
- `agents.heartbeat`
- `memory.events`
- `permission.requests`
- `iphone.requests`
- `feedback.events`
- `audit.events`

Plus tard: NATS JetStream si agents sur plusieurs machines, plus robuste, durable et observable.

## 8. iPhone Native Bridge

Le iPhone expose des capabilities, pas un accès brut:

- `phone.call.prepare`
- `message.sms.compose`
- `email.compose`
- `calendar.events.read`
- `calendar.event.create`
- `contacts.search`
- `location.current`
- `photos.pick`
- `photos.search.metadata`
- `camera.capture`
- `audio.record`
- `notification.schedule`
- `securestore.get/set`

Chaque capability a:

- permission iOS;
- permission monGARS;
- niveau de risque;
- besoin ou non d'approbation humaine;
- format de réponse minimal;
- politique de rétention.

## 9. Tests

Mobile:

- typecheck TypeScript;
- ESLint;
- Jest + React Native Testing Library;
- tests sync offline/online;
- tests permission denied;
- tests UI approval;
- tests pairing/auth;
- tests WebSocket reconnect.

Backend:

- pytest;
- ruff;
- mypy;
- bandit;
- JSON Schema validation;
- contract tests OpenAPI;
- message board integration tests;
- model response parser tests;
- audit ledger integrity tests.

E2E:

- iPhone simulator/dev client;
- Android emulator smoke path;
- WebSocket + task lifecycle;
- “agent asks for iPhone info” → iPhone prompts → user approves → result returned.

## 10. Definition of Done MVP

MVP accepté quand:

- l'iPhone peut se pairer à Ubuntu;
- l'app affiche chat/tasks/approvals/memory/settings;
- Ubuntu garde le state maître;
- iPhone garde une replica locale et outbox;
- orchestrateur produit des tool calls JSON validés;
- gateway demande permission au lieu de laisser le modèle bloquer;
- un worker sandbox exécute une action file-safe;
- event log et feedback log enregistrent tout;
- tests locaux passent.
