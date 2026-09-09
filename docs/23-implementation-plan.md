# 23 — Implementation Plan

> **Statut:** ordre de construction historique et architecture cible. Le contrat
> courant est l'OpenAPI `0.11.0`. `docs/21-acceptance-criteria.md` documente
> encore les preuves du slice `0.7` et ne valide pas à lui seul cette release.
> Redis Streams est un backend optionnel de notification; SQLite reste le
> défaut et la source de vérité. Le transport Redis authentifié et le failover
> de protocole ont une preuve bornée **QUALIFIED**; NATS, Postgres, TLS Redis,
> deux hôtes worker physiques et toute topologie SQLite active-active restent
> **PLANNED**, **EXPERIMENTAL** ou **UNSUPPORTED**, jamais une dépendance cachée
> du runtime.

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
- émission du ticket sous revalidation atomique du bearer et fermeture des
  sockets établis de l'ancienne lignée après re-pair;
- cache SQLite lié à l'origine et lecture hors ligne en mode sûr.
- outbox mobile pour feedback, épinglage de mémoire et chat sans tâche;
- reçus serveur `Idempotency-Key` liés au device et au digest exact;
- bootstrap autoritatif avant drain et abandon lors d'un changement de
  jumelage/origine.
- sérialisation des drains par origine, timeout borné et quarantaine locale des
  entrées invalides/permanentes afin qu'elles ne bloquent pas les suivantes;
- une réponse perdue après commit est résolue par le reçu d'idempotence serveur,
  jamais par une seconde mutation autoritative;
- approvals, grants, exécution de capability, composeurs et autres effets
  sensibles restent interdits et ne sont jamais rejoués automatiquement.

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

IMPLEMENTED en `0.11.0`:

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
- runner de lease de maintenance avec renouvellement avant 50 % du TTL,
  annulation sur perte et vérification fenced avant chaque lot de mutations;
- métriques d'expiration de claims, publications dupliquées et latence; une
  publication dépassant sa lease peut être rejouée et reste volontairement au
  moins une fois;
- singletons persistants pour les cumuls outbox et leases/retries/dead letters
  worker, afin que `/status` ne scanne pas les historiques append-only;
- Agent Cards et allowlist serveur de skills/protocole/capacity, réévaluée à
  l'inscription, au claim et avant redistribution;
- projection de politique worker SQLite à epoch: reload par compare-and-swap
  de l'epoch observé avant parsing, et relecture sous writer lock pour
  inscription, mise en file, claim et reaper, sans autorité du cache processus;
- quarantaine atomique des jobs queued dont le skill est révoqué. Une lease
  active peut finir, mais une lease révoquée expirée ne sera pas redistribuée;
- workers Files, Research à destination HTTPS publique épinglée et Code Review
  sur snapshot/repository identity revalidés, sans write, push, hooks, config
  Git hôte, récupération paresseuse ni shell générique;
- Research sous un deadline monotone global avec résolution DNS single-flight;
  Code Review sous un deadline global, snapshot total de 64 MiB et profondeur
  maximale de 64;
- Redis borné par `MAXLEN ~`, trim d'âge opportuniste à chaque nouvelle
  publication non dédupliquée, TTL d'inactivité du stream et clés de
  déduplication SHA-256 indépendantes à TTL `PX`; aucun historique infini ni
  index global n'est promis;
- scheduler v2 avec ratio de charge, score observé, latence qualifiée,
  ancienneté et tie-break stable; décision expurgée persistée;
- scoring transparent depuis résultats, expirations et feedback observés par le
  serveur;
- sérialiseur central de confidentialité pour WebSocket, board, Redis et
  outbox;
- `/status` authentifié avec identité, santé backend et compteurs seulement.

QUALIFIED en `0.11.0`:

- Redis authentifié réel sur `ubuntu-host` via tunnel SSH: 8 tests live pour
  auth, down/up, reprise de l'outbox, doublon après acceptation distante,
  timeout et rétention;
- harness à deux identités worker derrière un unique control plane
  SQLite: expiration de A, claim génération `N+1` par B, résultat stale de A
  refusé;

EXPERIMENTAL / UNSUPPORTED:

- le harness automatique valide le protocole, pas deux machines worker
  physiques;
- Redis TLS et le rejet d'un certificat invalide ne sont pas encore qualifiés;
- SQLite partagé via NFS/filesystem réseau et active-active autoritatif
  inter-hôtes sont non supportés.

PLANNED:

- qualification TLS/monitoring Redis et consommateurs Redis actifs;
- déploiement et preuve de workers sur plusieurs hôtes physiques;
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
- projection FAISS contrôlée par UID et modes privés, intégrité vérifiée,
  génération/pointeur atomiques, rétention configurable et reprise sûre après
  interruption ou manque d'espace;
- cleanup sous lock des seuls temporaires privés/possédés `.tmp-*`,
  `.CURRENT-*` et `.digest-*` laissés par un crash, avec refus des orphelins
  non sûrs;
- Memory API/UI existantes.

PLANNED:

- branchement/qualification de FAISS sur le chemin production complet;
- Qdrant et stratégie multi-hôte de l'index;
- enrichissement du chunker et du retrieval pack.

### Step 12 — iPhone bridge

IMPLEMENTED et conservé en `0.11.0`:

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
La tentative du 2026-09-08 est `BLOCKED/NOT RUN`: le guest QEMU n'énumérait pas
l'iPhone et ne disposait ni du tunnel CoreDevice ni des services DDI. Voir
`docs/evidence/iphone-validation-2026-09-08.md`; un build ou un test unitaire ne
remplace pas cette preuve.

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

### Step 15 — Production qualification v0.11

IMPLEMENTED:

- niveaux séparés `scripts/check.sh`, `scripts/check-integration.sh` et
  `scripts/check-chaos.sh`;
- harness Redis réel optionnel et smoke test multi-worker;
- scénarios chaos bornés pour publisher/control plane/worker interrompu, Redis
  down/up, publication lente, course reaper/heartbeat, révocation, doublon,
  outbox mobile et résultat natif incertain;
- diagnostics de transport/outbox/maintenance/leases/quarantaine/vector sans
  payload sensible;
- triage npm sans `--force` incompatible avec Expo SDK 57.

MANUAL VALIDATION REQUIRED:

- exécuter les six capabilities sur un iPhone physique avec permissions,
  refus, annulation, succès, background/reprise, expiration, pertes réseau et
  kill après effet;
- enregistrer une preuve structurée; l'hôte QEMU courant n'exposait aucun
  iPhone physique utilisable.

PLANNED:

- qualification Redis TLS/certificat invalide;
- preuve avec workers placés sur au moins deux machines physiques;
- supervision, sauvegarde et procédures d'incident production.

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
