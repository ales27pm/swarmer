# MASTER SPEC — monGARS Swarm App

Date: 2026-09-08
Statut: Draft build-ready

> **Portée:** architecture cible et frontières du slice `0.9.0`. Son contrat
> exécutable est décrit par l'OpenAPI. `docs/21-acceptance-criteria.md` conserve
> les preuves et limites du slice `0.7`; il n'est pas présenté comme validation
> de `0.9.0`. Le runtime livre REST authentifié, cache SQLite mobile lié à
> l'origine, WebSocket à ticket unique, exécution
> locale vérifiée, jobs distants à lease et transport de capabilities iPhone à
> grant unique. Le message board et son outbox transactionnelle sont tous deux
> implémentés en SQLite. L'outbox mobile, Redis/NATS multi-hôte, Postgres, les
> consumer groups et un index vectoriel externe restent **PLANNED**.

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
  ├─ Message Board + outbox SQLite
  ├─ State Service SQLite WAL
  ├─ Memory Service SQLite + embeddings optionnels
  ├─ Feedback Service
  ├─ Audit Ledger append-only
  └─ Worker Runtime(s)
       ↓
Agents distants
  ├─ Files worker de lecture (IMPLEMENTED)
  └─ Code/Research/CRM/Design/Phone workers (PLANNED)
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

- **IMPLEMENTED:** registre authentifié, worker Files de lecture, work queue,
  lifecycle, leases et message board/outbox SQLite au slice `0.9.0`.
- **PLANNED:** agent cards, autres classes de workers et flotte autonome
  multi-hôte.
- **PLANNED:** Redis Streams/NATS seulement lors d'une future migration
  multi-hôte.

### Phase 4 — Native iPhone Bridge

- **IMPLEMENTED:** Location, Contacts, lecture Calendar, picker Photos et
  composeurs Mail/SMS via broker, avec permission explicite et Approval UI.
- **PLANNED:** Reminders, écriture Calendar, Camera, Audio et autres
  capabilities.

### Phase 5 — Mémoire et feedback

- **IMPLEMENTED:** Memory Service SQLite, embeddings optionnels, Feedback
  Service et export JSONL revu.
- **PLANNED:** FAISS/Qdrant externe, eval builder complet et LoRA candidate
  pipeline.

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

- iPhone: réplica/cache SQLite implémentée; outbox de mutations encore planifiée.
- Ubuntu: SQLite WAL autoritatif au MVP; migration vers Postgres planifiée si
  les besoins opérationnels exigent plusieurs writers.
- Memory Service: chunking, embeddings, semantic search, metadata filters.
- Vector store externe: FAISS/Qdrant planifié; le runtime actuel conserve les
  vecteurs optionnels en SQLite et retombe sur la recherche lexicale.
- Event log: append-only pour replay et audit.

## 7. Message board

### IMPLEMENTED — slice `0.9.0`

Le board durable et l'outbox transactionnelle utilisent la même base SQLite que
l'état. Les transitions métier écrivent leur entrée d'outbox dans la même
transaction. Un drain la publie ensuite au moins une fois dans
`message_board_events`; `dedupe_key` empêche qu'un rejeu crée un second
événement. Ce mécanisme n'est pas un bus réseau et n'offre pas de consumer group
multi-hôte.

Topics actuellement produits:

- `tasks.inbox`
- `tasks.status`
- `agents.heartbeat`
- `iphone.capabilities`
- `agent.job.capability.result`

### PLANNED

Redis Streams ou NATS JetStream, consumer groups, partitionnement multi-hôte et
dead-letter stream externe. Aucun de ces composants n'est requis ni annoncé
comme branché dans le slice `0.9.0`.

## 8. iPhone Native Bridge

L'iPhone expose des capabilities, pas un accès brut.

### IMPLEMENTED — transport `0.9.0`

- `iphone.location.current`
- `iphone.contacts.lookup`
- `iphone.calendar.events`
- `iphone.photos.pick`
- `iphone.mail.compose`
- `iphone.sms.compose`

Une demande est liée à la tâche, au job, à l'agent, à la génération de lease et
à l'iPhone source. La Gateway crée une approbation courte. Après approbation,
l'iPhone reçoit le grant opaque lié au digest canonique seulement dans la
réponse d'autorisation. Il le consomme côté serveur avant tout appel natif, puis
soumet un résultat corrélé. Une réponse d'approbation perdue peut être reprise
avant consommation: le serveur fait tourner le grant et invalide le précédent.
Une lease expirée, une tâche annulée, un digest différent ou un rejeu de
consommation ferme la voie d'exécution. L'événement WebSocket initial contient
`request_id`,
`capability_name`, `expires_at` et `preview.arguments_redacted: true`; les
événements de mise à jour ne contiennent que `request_id`. Les arguments et le
grant sont récupérés via REST authentifié.
Les composeurs mail/SMS restent visibles et ne constituent pas un envoi
silencieux. Le code et les contrats automatisés sont implémentés; la preuve des
permissions et dialogues sur iPhone physique reste à produire.

Une génération de lease qui a créé une demande de capability n'est jamais
redistribuée automatiquement, quel que soit l'état atteint par cette demande:
l'absence de preuve terminale ne prouve pas l'absence d'effet natif. Sur mobile,
le workflow est lié à l'origine et au bearer d'appareil initiaux. Grants et
résultats sensibles restent éphémères; une mort du processus après l'action iOS
mais avant la remise du résultat laisse Ubuntu dans un état explicitement
incertain et n'autorise aucun rejeu automatique.

### PLANNED

- `phone.call.prepare`
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

Ces identifiants historiques ne sont pas des alias exécutables du contrat
`iphone.*` actuel. Toute capability future devra encore définir:

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
- iPhone garde une réplique/cache locale; aucune outbox mobile n'est prétendue
  livrée dans ce DoD;
- orchestrateur produit des tool calls JSON validés;
- gateway demande permission au lieu de laisser le modèle bloquer;
- un worker sandbox exécute une action file-safe;
- event log et feedback log enregistrent tout;
- tests locaux passent.
