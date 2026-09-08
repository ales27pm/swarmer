# MASTER SPEC — monGARS Swarm App

Date: 2026-09-08
Statut: Draft build-ready

> **Portée:** architecture cible et frontières du slice `0.10.0`. Son contrat
> exécutable est décrit par l'OpenAPI. `docs/21-acceptance-criteria.md` conserve
> les preuves et limites du slice `0.7`; il n'est pas présenté comme validation
> de `0.10.0`. Le runtime livre REST authentifié, cache SQLite mobile lié à
> l'origine, WebSocket à ticket unique, exécution
> locale vérifiée, jobs distants à lease et transport de capabilities iPhone à
> grant unique. Le message board SQLite reste le défaut; un adaptateur Redis
> Streams optionnel transporte les mêmes événements sans devenir autoritatif.
> Les claims de publication de l'outbox, l'identité des processus, les leases de
> maintenance, les consumers de confiance, l'outbox mobile sûre, trois workers
> bornés, les cartes/politiques agent, le scheduler/scoring v2 et une projection
> FAISS reconstruisible sont **IMPLEMENTED**. La qualification multi-hôte de
> production, Postgres, NATS, le raccordement opérationnel de consumers Redis et
> la validation iPhone physique restent respectivement **PLANNED** ou
> **MANUAL VALIDATION REQUIRED**.

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
  ├─ Outbox SQLite autoritative + claims de publication fenced
  ├─ Message Board SQLite par défaut / Redis Streams optionnel
  ├─ Consumer checkpoints + maintenance leases SQLite
  ├─ State Service SQLite WAL
  ├─ Memory Service SQLite + embeddings optionnels
  ├─ Feedback Service
  ├─ Audit Ledger append-only
  └─ Worker Runtime(s)
       ↓
Agents distants
  ├─ Files worker de lecture (IMPLEMENTED)
  ├─ Research worker à adaptateur borné (IMPLEMENTED)
  ├─ Code Review worker en lecture seule (IMPLEMENTED)
  └─ CRM/Design/Phone workers (PLANNED)
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
  lifecycle, leases de job et génération de fencing.
- **IMPLEMENTED:** claims de publication d'outbox atomiques, leases/générations
  de publisher, enveloppe durable à `dedupe_key` obligatoire, board SQLite et
  adaptateur Redis Streams optionnel.
- **IMPLEMENTED:** identité aléatoire par boot du control plane, heartbeats,
  leases singleton SQLite pour les boucles de maintenance et fondation de
  consumer de confiance avec ack après succès, retry borné et dead letter.
- **IMPLEMENTED:** cartes d'agents validées côté serveur, workers Research et
  Code Review bornés, scheduler/scoring déterministes, cutoff de fraîcheur des
  heartbeats et preuves de sélection. Le timeout est configuré par
  `MONGARS_AGENT_OFFLINE_TIMEOUT_SECONDS` et doit dépasser l'intervalle de
  heartbeat.
- **PLANNED:** qualification multi-hôte de production, déploiement/monitoring
  Redis et consumers Redis opérationnels. Les workers restent derrière les API
  authentifiées et ne consomment pas Redis directement.
- **PLANNED:** NATS JetStream seulement si un besoin concret le justifie.

### Phase 4 — Native iPhone Bridge

- **IMPLEMENTED:** Location, Contacts, lecture Calendar, picker Photos et
  composeurs Mail/SMS via broker, avec permission explicite et Approval UI.
- **PLANNED:** Reminders, écriture Calendar, Camera, Audio et autres
  capabilities.

### Phase 5 — Mémoire et feedback

- **IMPLEMENTED:** Memory Service SQLite, embeddings optionnels, Feedback
  Service et export JSONL revu.
- **IMPLEMENTED:** protocole d'index vectoriel et projection FAISS locale
  optionnelle, reconstruisible par commande depuis les embeddings SQLite.
- **PLANNED:** raccordement opérationnel de FAISS au chemin de requête, Qdrant,
  eval builder complet et pipeline candidat LoRA.

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

- iPhone: réplica/cache SQLite et outbox de mutations sûres implémentées. Cette
  outbox accepte seulement feedback, épinglage de mémoire et chat sans création
  de tâche; elle refuse les approbations, capabilities, composeurs, processus et
  toute action sensible.
- Ubuntu: SQLite WAL autoritatif au MVP; migration vers Postgres planifiée si
  les besoins opérationnels exigent plusieurs writers.
- Memory Service: chunking, embeddings, semantic search, metadata filters.
- Index vectoriel secondaire: FAISS local optionnel peut être reconstruit depuis
  les IDs/embeddings SQLite; SQLite demeure la source de vérité et le fallback
  lexical reste disponible. Qdrant reste planifié.
- Event log: append-only pour replay et audit.

## 7. Message board

### IMPLEMENTED — slice `0.10.0`

Les transitions métier écrivent leur entrée d'outbox dans la même transaction
SQLite que l'état. Chaque processus possède un `instance_id` aléatoire et doit
revendiquer les lignes avant publication avec une lease courte et une
`publish_generation`. Le marquage `published_at` est fenced par propriétaire,
génération et expiration: un ancien publisher ne peut pas confirmer le travail
d'un successeur. La livraison reste au moins une fois; `event_id` et
`dedupe_key` applicatifs rendent sûr un crash après publication mais avant le
marquage.

`SQLiteMessageBoard` est le backend par défaut. `RedisStreamsMessageBoard` est
un adaptateur optionnel de notification avec quatre familles de streams
(`tasks`, `agents`, `iphone`, `system`) et déduplication applicative atomique.
Une panne Redis dégrade la santé du transport mais laisse l'événement non publié
dans l'outbox pour une reprise ultérieure. Ni l'ID Redis ni le contenu d'un
stream ne devient autoritatif.

`ConsumerCheckpointStore` et `MessageConsumer` fournissent aux seuls services
de confiance du control plane une identité de consumer, un claim fenced, un ack
après handler réussi, un retry borné, une dead letter et un checkpoint
persistant. Cette fondation n'est pas encore un pipeline Redis déployé.

Topics actuellement produits:

- `tasks.inbox`
- `tasks.status`
- `agents.heartbeat`
- `iphone.capabilities`
- `agent.job.capability.result`

### PLANNED

Qualification Redis multi-hôte (topologie, TLS/auth, sauvegarde, monitoring,
reprise et consumer groups opérationnels), NATS JetStream, partitionnement
inter-régions et dead-letter stream externe. Aucun de ces éléments n'est une
condition cachée du mode SQLite par défaut, et cette release ne revendique pas
la production multi-hôte.

## 8. iPhone Native Bridge

L'iPhone expose des capabilities, pas un accès brut.

### IMPLEMENTED — transport conservé en `0.10.0`

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
silencieux. Les événements partagés sont maintenant sérialisés par un contrat
central fermé: aucune coordonnée, fiche contact, donnée calendrier/photo,
adresse ou corps de message ne passe dans WebSocket, board ou Redis. Le code et
les contrats automatisés sont implémentés.

### MANUAL VALIDATION REQUIRED

Les permissions, dialogues, annulations, arrière-plan/reprise, expiration,
perte réseau après effet et absence de rejeu doivent encore être observés sur
un iPhone physique selon `docs/26-iphone-physical-device-validation.md`. Aucun
build, test unitaire ou simulateur n'est présenté comme cette preuve.

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
- iPhone garde une réplique/cache locale et une outbox strictement limitée aux
  mutations ordinaires idempotentes; aucune action sensible n'y entre;
- orchestrateur produit des tool calls JSON validés;
- gateway demande permission au lieu de laisser le modèle bloquer;
- un worker sandbox exécute une action file-safe;
- event log et feedback log enregistrent tout;
- tests locaux passent.
