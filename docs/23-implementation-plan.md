# 23 — Implementation Plan

> **Statut:** ordre de construction historique et architecture cible. Le contrat
> courant est l'OpenAPI `0.9.0`. `docs/21-acceptance-criteria.md` documente
> encore les preuves du slice `0.7` et ne valide pas à lui seul cette release.
> Les mentions Redis/NATS ou Postgres ci-dessous sont **PLANNED**, jamais une
> dépendance cachée du runtime.

## Ordre cible historique

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

Livré localement avec tests de frontières simulées:

- Bootstrap.
- WebSocket.
- Reconnect handling.
- cache SQLite lié à l'origine et lecture hors ligne en mode sûr.

Roadmap:

- journal durable et Pull par curseur;
- Push idempotent;
- Outbox.

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
- outils supportés validés avant proposition et résultats d'exécuteur réels;
- aucune réussite simulée.

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

IMPLEMENTED en `0.9.0`:

- interface de board et implémentation SQLite durable;
- outbox transactionnelle, livraison au moins une fois et déduplication;
- protocole worker HTTP authentifié, capacité et scheduler déterministe;
- une job distante active par tâche;
- lease opaque hashée, id/génération/expiration, heartbeat et fencing;
- reaper: retry automatique borné seulement pour les lectures sans aucune
  activité de capability iPhone dans la génération expirée; sinon échec avec
  issue potentiellement incertaine et dead letter locale;
- heartbeat autoritatif à chaque renouvellement, publication durable coalescée
  par job et génération.

PLANNED:

- Redis Streams ou NATS JetStream multi-hôte;
- consumer groups réseau, partitionnement et dead-letter queue externe;
- ordonnanceur autonome au-delà de la claim déterministe.

### Step 11 — Memory

- Embedding model.
- Chunker.
- FAISS.
- Memory search API.
- Memory UI.

### Step 12 — iPhone bridge

IMPLEMENTED en `0.9.0`:

- `iphone.location.current`, `iphone.contacts.lookup`,
  `iphone.calendar.events`, `iphone.photos.pick`;
- `iphone.mail.compose` et `iphone.sms.compose` avec UI native visible;
- création/poll par worker sous lease;
- liste/détail/décision/consommation/résultat par l'iPhone ciblé;
- création identique dédupliquée par fingerprint; reprise d'approbation par
  rotation du grant non consommé;
- grant court à usage unique, digest exact et résultat idempotent;
- notification `iphone.capability.requested` avec identifiant, capability,
  expiration et marqueur d'arguments expurgés; mise à jour avec `request_id`
  seulement.

VALIDATION PENDING: permissions, dialogues et exécution native sur iPhone
physique; un build ou un test unitaire ne remplace pas cette preuve.

PLANNED:

- calendrier en écriture, caméra, audio, notifications, documents et appels;
- push externe et reprise multi-hôte.

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
- Toute nouvelle iPhone capability vient après sa règle Gateway, son schéma, ses
  tests et son UI d'approbation. Un worker ne reçoit jamais le bearer du device.

## First vertical slice

La première tranche utile est implémentée avec le worker de lecture:

> Depuis iPhone, demander “liste les fichiers du projet X”; Ubuntu crée tâche; worker lit seulement le dossier autorisé; retourne résultat; feedback enregistré.

Deuxième tranche, encore locale et soumise à approbation:

> Demander “modifie tel fichier”; worker propose diff; gateway demande permission; iPhone approuve; patch appliqué; tests lancés; audit enregistré.
