# 23 — Implementation Plan

> **Statut:** ordre de construction historique et architecture cible. Le contrat
> courant est l'OpenAPI `0.10.0`. `docs/21-acceptance-criteria.md` documente
> encore les preuves du slice `0.7` et ne valide pas à lui seul cette release.
> Redis Streams est un backend optionnel de notification; SQLite reste le
> défaut et la source de vérité. NATS, Postgres et la qualification multi-hôte
> de production restent **PLANNED**, jamais une dépendance cachée du runtime.

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
- outbox mobile pour feedback, épinglage de mémoire et chat sans tâche;
- reçus serveur `Idempotency-Key` liés au device et au digest exact;
- bootstrap autoritatif avant drain et abandon lors d'un changement de
  jumelage/origine.

Roadmap:

- journal durable et Pull par curseur;
- résolution générale de conflits multi-writer;
- nouvelles opérations offline uniquement après preuve d'idempotence et revue
  de sécurité.

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

IMPLEMENTED en `0.10.0`:

- interface `DurableEvent`, board SQLite durable par défaut et adaptateur Redis
  Streams optionnel;
- outbox transactionnelle avec claims concurrents, lease/génération de
  publication, fencing du marquage, livraison au moins une fois et
  déduplication applicative;
- reprise des claims expirés et panne Redis sans perte d'état;
- identité aléatoire/heartbeat de chaque instance du control plane;
- leases de maintenance singleton pour reaper, capability expirer, outbox et
  feedback/scoring;
- fondation consumer de confiance avec claim fenced, ack après succès, retry
  borné, dead letter et checkpoint SQLite;
- protocole worker HTTP authentifié, capacité et scheduler déterministe;
- une job distante active par tâche;
- lease opaque hashée, id/génération/expiration, heartbeat et fencing;
- reaper: retry automatique borné seulement pour les lectures sans aucune
  activité de capability iPhone dans la génération expirée; sinon échec avec
  issue potentiellement incertaine et dead letter locale;
- heartbeat autoritatif à chaque renouvellement, publication durable coalescée
  par job et génération.
- Agent Cards et allowlist serveur de skills/protocole/capacity, réévaluée à
  l'inscription, au claim et avant redistribution;
- workers Files, Research à adaptateur HTTPS borné et Code Review sans write,
  push ou shell générique;
- scheduler v2 avec ratio de charge, score observé, latence qualifiée,
  ancienneté et tie-break stable; décision expurgée persistée;
- scoring transparent depuis résultats, expirations et feedback observés par le
  serveur;
- sérialiseur central de confidentialité pour WebSocket, board, Redis et
  outbox;
- `/status` authentifié avec identité, santé backend et compteurs seulement.

PLANNED:

- qualification opérationnelle Redis multi-hôte et consommateurs Redis actifs;
- NATS JetStream, partitionnement et dead-letter queue externe;
- affectation push aux workers; ils continuent actuellement à claim via HTTP;
- préparation production multi-hôte et migration Postgres si plusieurs writers
  deviennent nécessaires.

### Step 11 — Memory

IMPLEMENTED:

- recherche lexicale et embeddings SQLite optionnels avec ranking hybride;
- protocole `VectorIndex`, adapter de test en mémoire et projection FAISS locale
  optionnelle;
- rebuild FAISS par génération depuis les IDs/embeddings SQLite autoritatifs;
- fallback lexical si l'index manque ou échoue;
- Memory API/UI existantes.

PLANNED:

- branchement/qualification de FAISS sur le chemin production complet;
- Qdrant et stratégie multi-hôte de l'index;
- enrichissement du chunker et du retrieval pack.

### Step 12 — iPhone bridge

IMPLEMENTED et conservé en `0.10.0`:

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

MANUAL VALIDATION REQUIRED: permissions, dialogues, refus/annulation, succès,
arrière-plan/reprise, expiration, perte réseau et non-rejeu sur iPhone physique.
Le protocole `docs/26-iphone-physical-device-validation.md` reste `NOT RUN`; un
build ou un test unitaire ne remplace pas cette preuve.

PLANNED:

- calendrier en écriture, caméra, audio, notifications, documents et appels;
- push externe et reprise multi-hôte.

### Step 13 — Feedback/evals

- Feedback UI et event store: IMPLEMENTED.
- Corrections, exemples d'eval et export JSONL expurgé: IMPLEMENTED.
- score agent observé et versionné: IMPLEMENTED.
- eval builder/curation et pipeline LoRA: PLANNED.

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
