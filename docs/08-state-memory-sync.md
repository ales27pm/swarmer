# 08 — State, Memory and Sync

> **IMPLEMENTED — slice `0.11.0`:** le bootstrap hydrate tâches, approbations, appels d'outils,
> conversations/messages, agents, mémoire épinglée, curseur et compteurs dans
> la réplica liée à l'origine. Elle demeure un cache et n'autorise aucune action
> sensible. Une outbox mobile distincte synchronise seulement trois mutations
> explicitement sûres et idempotentes. La recherche lexicale est conservée; un
> provider d'embeddings optionnel active un ranking hybride SQLite et une
> projection FAISS locale peut être reconstruite depuis SQLite. **PLANNED:**
> résolution de conflits générale, Qdrant et branchement opérationnel de FAISS
> dans toutes les recherches.

## Objectif

Donner à toutes les composantes le même état et une mémoire long terme sans que les agents écrivent n'importe où.

## Architecture data

```text
State Service
  ├─ SQLite WAL MVP / Postgres later
  ├─ append-only event log
  ├─ task/conversation/approval tables
  └─ sync cursors

Memory Service
  ├─ chunker
  ├─ embedding model
  ├─ SQLite embeddings (authoritative metadata)
  ├─ optional rebuildable FAISS projection
  ├─ metadata store
  └─ retrieval pack builder

iPhone Local Replica
  ├─ SQLite app DB
  ├─ safe mutation outbox scoped by origin
  ├─ synced cursors
  └─ cached resource projections (never capability grants)
```

## Source de vérité

Ubuntu est maître pour:

- task status;
- approvals;
- agent registry;
- long-term memory;
- audit log;
- feedback;
- artifacts.

L'iPhone est maître temporaire pour:

- draft local;
- mutations ordinaires non synchronisées qui appartiennent encore exactement à
  l'origine et au contexte de jumelage capturés;
- permissions iOS runtime;
- capteur/donnée native demandée;
- UI state.

## SQLite iPhone tables

```sql
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sync_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE mutation_outbox (
  id TEXT PRIMARY KEY,
  origin TEXT NOT NULL,
  operation TEXT NOT NULL,
  resource_id TEXT,
  payload_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  last_error TEXT,
  completed_at TEXT,
  UNIQUE(origin, idempotency_key)
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  risk TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

## Ubuntu DB tables MVP

Voir `docs/13-data-model.md` pour le schéma complet.

Tables principales:

- `tasks`, `conversations`, `messages`, `tool_calls`, `approvals`;
- `agents`, `agent_jobs`, `scheduler_decisions`, `agent_score_snapshots`;
- `outbox_events`, `message_board_events`, deliveries/checkpoints consumers;
- `control_plane_instances`, `maintenance_leases`;
- `memory_items`, `memory_embeddings`;
- `feedback_events`, `eval_examples`, `corrections`, `agent_scores`;
- tables de pairing, demandes/grants/résultats iPhone, reçus d'idempotence et
  `audit_events`.

## Memory layers

### Session context

Court terme, dans la conversation active.

### State context

Données structurées: tâches, agents, permissions, projets.

### Semantic memory

Chunks textuels vectorisés:

- conversations importantes;
- décisions;
- fichiers résumés;
- corrections utilisateur;
- préférences;
- project facts.

`memory_embeddings` conserve les vecteurs associés à des IDs de mémoire stables.
L'interface `VectorIndex` traite tout index externe comme une projection
reconstruisible. L'adaptateur FAISS optionnel écrit une nouvelle génération puis
permute atomiquement son pointeur `CURRENT.json`; une génération absente ou
corrompue n'efface aucun item SQLite. En `0.11.0`, il vérifie l'UID propriétaire,
les modes privés, l'absence de symlink/hardlink et l'intégrité SHA-256 du root,
du pointeur, des générations et des fichiers. Une reconstruction interrompue ou
un disque plein avant la permutation préserve la génération active; les anciennes
générations sont nettoyées avec une rétention bornée par
`MONGARS_VECTOR_INDEX_GENERATIONS_TO_KEEP`. La commande
`python -m app.commands.rebuild_vector_index --db ... --provider ... --index-path ...`
reconstruit cette projection. Le backend FAISS et ses dépendances restent
optionnels; le fallback lexical demeure utilisable.

Avant une nouvelle génération, le rebuild tient le lock de projection et traite
les restes d'un crash: il valide puis supprime seulement les répertoires
`.tmp-*` et fichiers `.CURRENT-*`/`.digest-*` privés, réguliers et appartenant à
l'UID courant. Un nom inattendu, un mauvais propriétaire/mode ou un lien fait
échouer le cleanup sans suivre ni effacer l'artefact.

### Artifact memory

Fichiers/disques:

- patches;
- rapports;
- exports;
- captures;
- logs.

### Event log

Append-only:

- qui a fait quoi;
- quand;
- permission;
- résultat;
- hash précédent.

L'API projette ce journal avant lecture et expurge notamment les anciens textes
d'erreur de processus. La chaîne de hash couvre toujours le payload interne brut
immuable, pas sa projection publique éventuellement expurgée.

## Memory write policy

Les agents ne peuvent pas écrire directement une mémoire permanente. Ils proposent:

```json
{
  "type": "memory_write_candidate",
  "scope": "project:27pm-crm",
  "content": "Le projet préfère les checks locaux plutôt que GitHub Actions.",
  "confidence": 0.92,
  "source_event_id": "evt_...",
  "sensitivity": "normal"
}
```

Memory Service décide:

- auto-store low-risk project facts;
- ask user for personal/sensitive facts;
- reject duplicates;
- merge/update if needed.

## Retrieval policy

Quand l'orchestrateur démarre une tâche:

1. Lire state structuré.
2. Chercher mémoire vectorielle avec query.
3. Filtrer par scope.
4. Construire context pack limité.
5. Citer les memory ids dans le task log.

## Sync strategy

### Bootstrap

```http
GET /sync/bootstrap
```

Retourne:

- heure serveur;
- tâches, approbations et appels d'outils;
- conversations et messages récents;
- agents et mémoire épinglée;
- compteurs et curseur d'audit.

Le client applique le bootstrap uniquement à la partition SQLite correspondant
à l'origine active. Après reconnexion WebSocket, il effectue ce bootstrap REST
autoritatif avant de drainer les mutations ordinaires en attente.
Les événements `task.*`, `message.*` et `approval.*` sont des invalidations
minimales avec `refetch_required: true`: ils ne contiennent ni intention/titre
de tâche, contenu de message, note utilisateur ni snapshot d'action. La réplica
ne les écrit donc pas comme des ressources complètes; le provider live regroupe
les invalidations en une relecture REST authentifiée et liée à l'origine. Une
seule relecture est active à la fois; trois reprises différées au maximum
rattrapent une panne transitoire. Un changement d'origine annule attente et
relecture anciennes avant toute application locale.

Les mutations SecureStore qui installent ou reprennent une connexion sont
sérialisées. La promotion et la suppression d'un candidat pending comparent sa
valeur sérialisée exacte et sa génération locale; une ancienne réponse réseau
ne peut donc ni écraser le re-pair courant ni en supprimer la trace de reprise.

### Mutation push idempotent — IMPLEMENTED

```http
POST /sync/mutations
Authorization: Bearer <device-token>
Idempotency-Key: <opaque-client-id>
```

Le serveur accepte une seule mutation par requête et persiste atomiquement le
résultat avec un reçu lié à l'appareil, à la clé, à l'opération et au digest
canonique exact. Une réponse perdue peut être demandée de nouveau avec la même
clé; une autre charge sous cette clé est refusée.

Allowlist mobile:

- `feedback.create`;
- `memory.metadata.update`, uniquement le booléen `pinned` d'un item existant;
- `chat.message.create`, uniquement avec `start_task: false`.

La ligne locale conserve origine, opération, ressource éventuelle, payload,
clé d'idempotence, tentatives et issue. Avant chaque POST, le client revérifie
que l'origine et le bearer actifs sont exactement ceux de la session de drain.
Lorsqu'un nouveau jumelage est finalisé et son bootstrap actif validé, les
mutations de l'ancienne origine sont marquées abandonnées plutôt que rejouées
sur le nouveau serveur.

### Qualification de rejeu mobile — QUALIFIED

- les drainers sont sérialisés par origine, y compris entre instances du runtime
  dans le même processus;
- un sender bloqué est borné à 15 secondes par défaut et 120 secondes maximum;
- une réponse transitoire ou perdue garde la même mutation et la même clé
  d'idempotence; aucun second envoi concurrent n'est démarré tant que l'issue du
  premier appel reste incertaine dans le processus;
- une ligne locale invalide ou un refus serveur permanent est quarantiné sans
  bloquer les mutations sûres suivantes;
- un changement d'origine ou de jumelage abandonne l'ancienne file et empêche
  le démarrage d'un nouvel envoi sous l'ancien contexte.

Ces garanties ont des tests de redémarrage, perte de réponse, course de drain et
changement d'origine. Elles ne transforment pas l'outbox mobile en canal pour
effets sensibles.

### Exclusions de l'outbox — INVARIANT

Ne sont jamais mis en attente hors ligne:

- décisions d'approbation ou actions de la Permission Gateway;
- création, autorisation, consommation ou résultat de capability iPhone;
- composition mail/SMS et autres effets natifs;
- `process.run`, writes, push ou création automatique de tâche;
- toute mutation dont l'idempotence externe n'est pas définie.

La réplica, un WebSocket reçu ou une permission iOS locale ne suffit jamais à
autoriser une action sensible.

### Conflict resolution

- Server wins: task status, approval status, audit.
- Client wins: local drafts, UI preferences.
- Merge: messages, feedback, memory corrections.
- Ask user: conflicting permission rules.

Une résolution générale multi-writer reste **PLANNED**. Le slice courant
implémente seulement l'idempotence exacte des trois opérations ci-dessus et la
préférence serveur pour tout état de sécurité.

## Backups et reprise

- **PLANNED:** automatisation et preuve opérationnelle d'un backup SQLite
  quotidien avec restauration testée.
- **IMPLEMENTED:** projection FAISS reconstruisible; sauvegarder d'abord SQLite et ses
  embeddings autoritatifs.
- **IMPLEMENTED:** chaîne d'audit append-only et export JSONL du dataset;
  politiques de rotation/rétention d'exploitation encore **PLANNED**.
