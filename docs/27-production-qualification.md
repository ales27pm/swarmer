# 27 — Production Qualification v0.11 and v0.12 delta

## Verdict de portée

La release `0.11.0` qualifie les invariants de panne du runtime; elle ne déclare
pas une architecture active-active ni une validation iPhone physique.

La release `0.12.0` ajoute le runtime de buts autonomes au-dessus de ces
invariants. Elle n'étend pas la topologie qualifiée: Ubuntu/SQLite reste
autoritatif, Redis reste une notification et les workers restent derrière les
API authentifiées.

## Delta de qualification v0.12

### IMPLEMENTED

- contrats JSON stricts planner/evaluator, DAG acyclique, validation de
  skill/policy et fingerprints sémantiques;
- budgets persistés de pas, parallélisme, replans, durée et appels modèle;
- tâche enfant par nœud worker, callbacks corrélés de claim/résultat/capability,
  annulation et refus d'un résultat stale par les invariants v0.11;
- résultat final déterministe depuis des résumés expurgés et provenance SQLite;
- contextes à limites indépendantes, épisodes résumés, retrieval de stratégies
  sans copie de plan et exports JSONL revus;
- réplica mobile de buts/nœuds/résultats et commandes désactivées sur cache
  hors ligne;
- scénarios automatisés de plan parallèle, failure, `needs_user`, lease stale,
  annulation, budget, boucle, mémoire épisodique et preuves contradictoires.

### QUALIFIED — portée automatisée

Les tests v0.12 qualifient les contrats et transitions déterministes dans le
harness local. Ils ne constituent pas une nouvelle preuve de déploiement
physique, de charge, de haute disponibilité ou d'effet iOS. Les chiffres du gate
final v0.12 doivent être consignés par la vérification de release; les chiffres
ci-dessous restent explicitement ceux de v0.11.

### EXPERIMENTAL / NOT QUALIFIED

- plusieurs Goal Managers actifs traitant le même but au-delà des verrous
  processus et de la réconciliation au démarrage;
- endurance/charge de plusieurs buts et modèles locaux simultanés;
- synthétiseur LLM distinct, routage dynamique par rôle et injection des cartes
  de contexte dans le payload evaluator;
- API publique, lifecycle opérateur et index FAISS pour la mémoire épisodique;
- pipeline de curation, entraînement et comparaison de modèles.

### MANUAL VALIDATION REQUIRED

La matrice iPhone physique reste inchangée: location, contacts, calendrier,
photo, mail et SMS sont toujours `BLOCKED/NOT RUN` dans l'environnement QEMU.
La nouvelle UI Swarm n'est pas une preuve de ces effets natifs.

### SUPPORTED

- un control plane Ubuntu autoritatif avec SQLite sur stockage local supporté;
- plusieurs workers distants, étroits et authentifiés par l'API du control plane;
- board SQLite par défaut ou Redis Streams optionnel comme notification;
- perte/reprise du broker sans perte de l'état métier;
- rejeu borné des seules lectures explicitement sûres;
- outbox mobile pour trois mutations ordinaires idempotentes, jamais pour un
  effet sensible.

### QUALIFIED

- perte et takeover d'une lease de maintenance pendant un travail long;
- publication plus longue que sa lease, duplicate at-least-once et fencing de
  l'ancien publisher;
- Redis authentifié réel sur `ubuntu-host` via tunnel SSH: huit tests live;
- deux identités worker contre un même control plane, mort du worker A, claim
  génération N+1 par B et rejet du résultat stale de A;
- révocation de skill, quarantine et absence de redistribution;
- policy worker SQLite à epoch/CAS: un reload parsé avant une révocation ne peut
  pas restaurer l'ancien allow, et les chemins inscription/file/claim/reaper ne
  prennent aucune décision depuis un cache processus périmé;
- émission atomique des tickets WebSocket et invalidation des sessions déjà
  établies lors d'un re-pair;
- durcissement snapshot/réseau des workers;
- stockage FAISS rebuildable avec ownership, modes, intégrité, rétention et
  reprise avant permutation;
- courses/rejeux de l'outbox mobile et filtres de confidentialité partagés.

### EXPERIMENTAL / NOT QUALIFIED

- Redis TLS et rejet d'un certificat invalide dans le harness live;
- deux workers réellement placés sur deux hôtes physiques distincts;
- haute disponibilité Redis et consumers Redis de production;
- FAISS utilisé comme projection opérationnelle dans tous les chemins de recherche;
- tout effet natif sur iPhone physique.

### UNSUPPORTED

- plusieurs control planes écrivant le même fichier SQLite sur NFS ou un
  filesystem réseau;
- active-active SQLite entre hôtes;
- Redis ou NATS comme source de vérité;
- workers connectés directement à Redis ou à l'iPhone;
- rejeu automatique d'un effet natif incertain.

## Fencing continu de maintenance

`MaintenanceLeaseRunner` acquiert une lease nommée, démarre un renouvellement
avant 50 % de son TTL, expose sa génération et annule le travail si le
renouvellement échoue. Chaque batch autoritatif revérifie
`require_current_locked()` à l'intérieur de sa transaction SQLite. Un owner de
génération N qui reprend après une pause ne peut donc pas muter après le takeover
N+1.

Les boucles couvertes sont:

- `agent-lease-reaper`;
- `capability-expirer`;
- `outbox-maintenance`;
- `feedback-maintenance` pour les scores.

La reconstruction vectorielle n'est pas lancée automatiquement. Lorsqu'elle est
appelée manuellement, elle construit une projection privée puis permute son
pointeur atomiquement.

## Outbox: sémantique de panne

La garantie reste **at-least-once**, jamais exactly-once.

Scénario qualifié:

1. le publisher N claim une entrée;
2. le broker bloque au-delà du TTL de publication;
3. le broker accepte finalement l'événement;
4. le `mark_published` de N est fencé localement;
5. N+1 reprend et republie le même `event_id`/`dedupe_key`;
6. le backend reconnaît le duplicate et l'état converge.

Les événements non publiés ne sont jamais supprimés. Les cumuls d'expiration,
duplicate et latence outbox, ainsi que les expirations/retries/dead letters des
jobs, sont des singletons SQLite persistants mis à jour avec leurs transitions.
`/status` les lit sans scanner les historiques append-only; aucune donnée de
payload ou credential n'y apparaît.

## Redis réel et rétention

Le harness `scripts/test-redis-integration.sh` sait utiliser Docker/Podman, un
endpoint fourni, ou Docker sur un hôte SSH avec tunnel local. La session de
qualification v0.11 a exécuté huit tests contre un Redis protégé par mot de passe
sur `ubuntu-host` via tunnel SSH. Elle couvre authentification, panne/reprise,
backlog, timeout/ack incertain, déduplication et trimming.

La rétention est bornée par:

- `MONGARS_REDIS_STREAM_MAXLEN`, avec trim approximatif;
- `MONGARS_REDIS_STREAM_RETENTION_SECONDS`: `MINID ~` est appliqué au stream
  visé à chaque nouvelle publication non dédupliquée et son TTL d'inactivité
  est renouvelé. Chaque `dedupe_key` utilise une clé indépendante nommée avec
  son SHA-256 et munie de son propre TTL `PX`; aucun hash ou index global n'est
  conservé.

Il n'existe pas de sweeper temporel continu: le trim d'âge est opportuniste à
la publication. Le stream et les clés de déduplication expirent selon leurs TTL
renouvelés indépendamment. Redis n'est donc jamais un historique infini.

La perte d'un historique Redis n'efface aucun état SQLite. Un consumer dont le
checkpoint est devenu trop ancien doit reconstruire sa projection depuis
l'état/API autoritatifs. TLS et invalid-certificat restent non qualifiés en live.

## Topologie multi-hôte réellement revendiquée

`scripts/run-multihost-smoke.sh` qualifie le protocole avec un control plane
SQLite autoritatif et deux identités worker indépendamment authentifiées. Le
harness vérifie le failover de lease et le fencing, mais il s'exécute dans une
topologie de processus de test. Il ne constitue pas une preuve que les deux
workers étaient sur deux machines physiques distinctes.

Le sens supporté de «multi-hôte» est donc: un Ubuntu autoritatif et des workers
qui peuvent se trouver sur d'autres machines via HTTPS. Une exécution smoke sur
deux hôtes worker physiques reste à consigner avant de qualifier ce placement.

## Révocation de skill

La politique est déterministe:

- job `queued` devenue interdite: `quarantined`, jamais claimable;
- lease déjà valide: autorisée à terminer selon la décision prise à la claim;
- lease expirée après révocation: `quarantined`, jamais redistribuée;
- audits `agent.job.skill_revoked` et `agent.job.quarantined`;
- aucun choix d'autorisation confié à un modèle.

La projection de règles est autoritative dans SQLite. Le reload capture l'epoch
avant parsing puis utilise un compare-and-swap sous `BEGIN IMMEDIATE`; un
candidat stale est refusé. Registration, mise en file, claim et reaper relisent
la même ligne dans leur transaction, de sorte qu'un cache allow stale ne peut
pas autoriser et qu'un cache deny stale ne peut pas écraser un allow durable.

## Workers

Le Files Worker demeure limité à list/read sous sa racine.

Le Research Worker conserve une URL HTTPS opérateur fixe et refuse les URLs avec
credentials, tout redirect, downgrade HTTP, cible loopback/privée/link-local,
rebinding DNS, réponse surdimensionnée/lente et décompression non
bornée. Un deadline monotone unique couvre DNS, connexion, TLS, requête,
headers et lecture. La résolution DNS est single-flight: après un timeout, un
second resolver ne s'empile pas derrière le thread bloqué. Les résultats web
restent du contenu externe non fiable.

Le Code Review Worker fixe l'identité de la racine et de `.git`, refuse
symlinks/hardlinks/alternates/grafts et configuration Git exécutable, copie les
fichiers sélectionnés dans un snapshot privé borné et exécute seulement des argv
fixes sans shell, hook, prompt, pager, helpers externes, replacement objects ni
push. Snapshot et commandes partagent un deadline monotone; le snapshot est
borné à 64 MiB au total, 16 MiB par fichier, 250 000 entrées et 64 niveaux. Une
isolation de production doit encore monter la racine en lecture seule au niveau
OS/conteneur: aucun contrôle userspace ne remplace cette frontière.

## Projection FAISS

SQLite et `memory_embeddings` restent autoritatifs. FAISS refuse une racine,
un pointeur ou des fichiers avec mauvais UID/modes, symlink, hardlink ou digest
invalide. Une génération est écrite en privé, vérifiée, puis rendue active par
permutation atomique. Un échec disque ou une interruption avant la permutation
préserve l'ancienne génération. La rétention est configurée par
`MONGARS_VECTOR_INDEX_GENERATIONS_TO_KEEP`; l'index peut être reconstruit depuis
SQLite. Au début d'un rebuild verrouillé, les seuls orphelins de crash supprimés
sont les `.tmp-*`, `.CURRENT-*` et `.digest-*` privés et possédés par l'UID
courant; tout artefact correspondant mais non sûr fait échouer le cleanup sans
être suivi ni effacé.

## Re-pair et transport WebSocket

La création d'un ticket WebSocket revalide dans la même transaction le bearer
actif et la lignée `last_pairing_id`; une requête ancienne bloquée avant le lock
ne peut pas émettre un ticket après le remplacement du jumelage. Le re-pair
supprime les tickets non consommés. Le champ durable
`devices.websocket_connection_id` impose un seul propriétaire de livraison par
appareil, y compris entre processus. Les sockets déjà ouvertes restent associées
à leur lignée et leur identifiant de connexion, revérifiés avant tout envoi;
elles sont évincées dès que l'un des deux n'est plus autoritatif. Envoi et
fermeture sont bornés par `MONGARS_WEBSOCKET_IO_TIMEOUT_SECONDS`, de sorte qu'un
pair lent ne retient pas indéfiniment le verrou ni la bascule de credential. Le
fan-out inter-appareils est concurrent.

Un journal SQLite uniquement métadonnées relaie les invalidations entre
processus, avec checkpoint par instance. Le nettoyage conserve toute ligne non
franchie par une instance dont le heartbeat est frais; il retire les checkpoints
arrêtés/orphelins/expirés. Une instance revenue après expiration reçoit
`sync.invalidated` et reconstruit depuis REST. Les demandes iPhone réémises à
la connexion sont limitées à `waiting_approval`/`approved`: un état `consumed`
n'est jamais rejoué. Le poll est réglé par
`MONGARS_WEBSOCKET_NOTIFICATION_POLL_SECONDS`; le seuil de checkpoint obsolète
par `MONGARS_WEBSOCKET_NOTIFICATION_INSTANCE_STALE_SECONDS`, qui doit dépasser
le heartbeat du control plane.

## Outbox mobile et effets sensibles

Les drains sont sérialisés par origine, les appels sont bornés dans le temps et
les erreurs permanentes/rows invalides sont quarantinées sans bloquer la suite.
Une réponse perdue réutilise la même clé d'idempotence. Une course de re-pair
abandonne l'ancienne origine. Les écritures SecureStore sont sérialisées et la
promotion/suppression d'une trace pending compare sa valeur exacte et sa
génération; une reprise ancienne échoue avant de toucher la nouvelle connexion.
Approbations, grants, résultats de capability, mail/SMS et `process.run` restent
hors de cette outbox.

Un résultat natif dont le POST est perdu peut seulement être resoumis dans le
même processus sans réexécuter l'effet. Après perte du processus, l'issue reste
incertaine et n'est jamais rejouée automatiquement.

## Qualification iPhone physique

**MANUAL VALIDATION REQUIRED.** Le 2026-09-08, le guest QEMU n'énumérait aucun
iPhone USB; les appareils Xcode visibles étaient en cache hors ligne, sans
tunnel CoreDevice ni DDI. Les six capabilities sont `BLOCKED/NOT RUN`, pas
`PASS`. Voir `docs/evidence/iphone-validation-2026-09-08.md`.

## Niveaux de vérification

### Replay du baseline v0.10

Le commit de départ `e7ca70166462e1133cb621aaad5e0575817fd146` a été
rejoué dans un worktree détaché avec les mêmes dépendances installées. Le replay
n'était pas entièrement vert: 215 tests Jest, Expo Doctor 21/21, 417 tests
serveur (1 skip), 85 tests workers, mypy, Bandit et OpenAPI ont réussi, mais le
test Swift d'annulation a reproduit son polling intermittent (5/6) et la version
courante de Ruff a signalé un import non trié dans
`test_outbox_concurrency.py`. La couture Swift déterministe et le formatage du
test font partie du delta v0.11; le baseline n'est donc pas présenté comme un
gate réussi rétrospectivement.

### Arbre v0.11 final

| Niveau | Commande/artefact | Statut v0.11 |
|---|---|---|
| UNIT | `scripts/check.sh` | PASS: 622 serveur, 137 workers, 238 Jest, Swift 6/6, Expo Doctor 21/21 |
| INTEGRATION | `scripts/check-integration.sh` | PASS: 16 transport, 1 skip conditionnel, 1 smoke multi-worker et 8 Redis live |
| REDIS LIVE | `scripts/test-redis-integration.sh` | QUALIFIED: 8 tests authentifiés sur `ubuntu-host`; TLS non exécuté |
| MULTI-WORKER | `scripts/run-multihost-smoke.sh` | QUALIFIED protocole/processus; placement multi-machine NOT RUN |
| CHAOS | `scripts/check-chaos.sh` | PASS: 50 scénarios serveur et 26 scénarios outbox mobile |
| PHYSICAL | `docs/26-iphone-physical-device-validation.md` | BLOCKED/NOT RUN |

Sorties fraîches du dernier gate sur l'arbre v0.11 final:

- serveur pytest: 622 réussis, 9 skips conditionnels, 2 avertissements de
  dépendances;
- workers: 137 réussis;
- mobile Jest: 23 suites, 238 tests réussis;
- Swift local model store: 6/6;
- Expo Doctor: 21/21;
- Ruff format/check: réussi; mypy strict: réussi sur 41 fichiers applicatifs et
  les quatre entrées workers/scripts; Bandit: aucune issue; OpenAPI: 41 chemins,
  45 opérations, 329 références et 7 JSON Schemas; Fallow: réussi.

## NPM audit

Le triage détaillé est dans `docs/security/npm-audit-v011.md`. L'état observé
est 13 advisories modérées, aucune haute/critique. Aucun `npm audit fix --force`
incompatible avec Expo SDK 57 n'est accepté comme remédiation.

## Risques résiduels

- validation Redis TLS/PKI et haute disponibilité non exécutée;
- topologie de workers sur machines réellement distinctes non prouvée;
- iPhone physique entièrement non exécuté dans cet environnement QEMU;
- SQLite partagé via filesystem réseau explicitement non supporté;
- snapshots Code Review à compléter par un montage OS réellement read-only;
- sauvegarde/restauration SQLite opérationnelle et alerting externe à qualifier.
