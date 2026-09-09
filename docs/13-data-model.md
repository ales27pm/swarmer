# 13 — Data Model

## Autorité et version

Le schéma implémenté est SQLite WAL et son marqueur exécutable autoritatif est
`SCHEMA_VERSION` dans `server/app/services/state_service.py`; la documentation
ne duplique pas ce numéro interne, qui est distinct de la version API/release
`0.12.0`. Les migrations sont additives et rejouables au démarrage. Postgres,
NATS et le câblage de base de données du prototype Vibecode ne sont pas branchés
au MVP. Redis Streams est un transport de notification optionnel, pas une base
autoritative et ne remplace aucune table ci-dessous.

Les migrations historiques ajoutent sans reconstruction destructive le board
et les jobs, l'outbox et ses claims fenced, les leases worker, le transport
iPhone, les identités/leases de maintenance, les consumers de confiance, les
reçus d'idempotence, les cartes/scores du scheduler, les compteurs
opérationnels persistants et la politique worker à epoch. Il ne s'agit pas d'un
déplacement de l'autorité: outbox, state, leases et receipts restent dans
SQLite.

La migration v0.12 ajoute les buts, DAG, appels/évaluations de modèle, résultats
agrégés, contextes, feedback et épisodes. Ces tables restent dans le même SQLite
autoritatif; elles ne transforment ni Redis ni un index vectoriel en source de
vérité.

Le fichier SQLite et la politique d'exécution doivent rester hors du workspace
monté en écriture dans Bubblewrap.

## Tables implémentées

### `tasks`

Identité, titre, intention, mode, `source` dérivée du principal authentifié,
conversation, état, priorité, timestamps de création/mise à jour/fin et erreur
structurée. Les états terminaux ne peuvent pas être réouverts.

### `conversations` et `messages`

Les conversations portent un titre et leurs timestamps. Chaque message conserve
conversation, tâche éventuelle, rôle, agent éventuel, contenu, métadonnées et
date. Une intention envoyée par `/chat` crée le message utilisateur et la tâche
dans la même transaction.

### `tool_calls` et `approvals`

Un appel d'outil conserve arguments JSON, résumé, risque, état, résultat ou
erreur et l'identifiant d'approbation éventuel. Un index unique interdit qu'une
approbation soit liée à plusieurs appels. Ces arguments exacts sont internes;
les projections API, bootstrap et WebSocket masquent le contenu d'écriture et
les arguments arbitraires de processus. Les résultats de processus restent eux
aussi internes; leurs projections publiques remplacent `stdout` et `stderr` par
des marqueurs de taille et généralisent le détail d'erreur.

Une approbation conserve tâche, identifiant d'appel, action, empreinte canonique
de l'identifiant/outil/arguments, résumé non fiable, risque, état, expiration,
décision et timestamps. Elle fige aussi le demandeur authentifié, la règle et le
motif de politique, un résumé expurgé des données touchées et l'identifiant de
son événement `approval.requested`. Pour une action sensible, événement d'audit,
tâche, appel et approbation sont créés ou transitionnés atomiquement. La décision
et la revendication recalculent la liaison et vérifient le contexte d'audit, puis
comparent et modifient atomiquement les trois états; annulation et expiration
invalident l'appel en attente. Une migration ne fabrique pas ce contexte pour les
anciennes lignes: toute demande historique encore en attente est annulée et
échoue fermée.

La consommation d'une approbation et son événement `approval.decided` partagent
une transaction; un refus y ajoute `tool.denied`. Les transitions d'annulation
et d'expiration partagent de même leur transaction avec, respectivement,
`task.cancelled` et `approval.expired`. Un état durable `completed` ou `failed`
et son événement terminal d'outil sont aussi validés et commis ensemble. L'effet
externe précède nécessairement cette dernière transaction; si la persistance
terminale échoue, l'état n'est pas présenté comme réussi et la récupération au
redémarrage le classe comme résultat incertain.

Au démarrage, toute paire tâche/appel laissée `running` est fermée en `failed`,
auditée par `execution.interrupted` avec un résultat incertain et n'est pas remise
en file. Un appel sensible encore `queued`, avec son approbation consommée et sa
tâche `queued`, est antérieur à la revendication atomique qui précède tout envoi:
il est fermé sans exécution ni rejeu et audité par `execution.not_started`. La
transition de récupération et son événement partagent la même transaction; un
échec d'audit laisse l'état récupérable au prochain redémarrage.

### `pairing_codes`, `pairing_candidates`, `devices` et `websocket_tickets`

- `pairing_codes.code` contient un HMAC, jamais le code à six chiffres en clair;
- un seul code est actif, avec expiration et compteur de tentatives;
- `pairing_candidates.token_hash` conserve seulement le digest du candidat court;
  ses états `staged` puis `finalized` restent liés au `pairing_id` et au
  `device_id`, et expirent sans modifier le jeton actif;
- `devices.token` contient `sha256:<digest>` d'un jeton aléatoire opaque;
- `devices.websocket_connection_id` désigne l'unique propriétaire courant des
  livraisons WebSocket pour l'appareil. Un nouvel établissement le remplace
  atomiquement; une autre instance qui conserve l'ancienne socket ne peut plus
  lui envoyer de contenu;
- la première authentification ordinaire du candidat finalisé remplace le jeton
  actif dans la même transaction et supprime ses tickets WebSocket inutilisés;
- `websocket_tickets.ticket_hash` contient le digest d'un ticket court,
  consommable une fois;
- la création d'un ticket relit le token actif et `last_pairing_id` dans la
  même transaction `BEGIN IMMEDIATE` que son insertion. Un ancien bearer bloqué
  avant le writer lock ne peut donc pas créer un ticket après qu'un re-pair a
  remplacé sa session;
- un WebSocket établi reste lié au `last_pairing_id` et au
  `websocket_connection_id` courants du device. Un re-pair invalide donc aussi
  la session déjà ouverte au prochain envoi ou message, pas seulement ses
  futurs tickets.

### `websocket_notifications` et `websocket_notification_checkpoints`

Le premier est un journal SQLite best-effort de projections WebSocket
uniquement métadonnées; il ne contient ni contenu utilisateur ni résultat
natif. Chaque processus possède un checkpoint de boot indépendant. Une ligne
n'est nettoyée qu'après le minimum de tous les checkpoints d'instances encore
vivantes. Les checkpoints arrêtés, orphelins ou dont le heartbeat est expiré
sont supprimés; si l'ancienne instance revient, elle émet une invalidation
globale et reconstruit par REST plutôt que de rejouer un effet. Un arrêt propre
supprime son propre checkpoint.

### `agents`

Nom, version, endpoint, modèle, skills, état, heartbeat et digest de la
credential dédiée. Un agent débute `unverified`; un heartbeat authentifié peut
ensuite le déclarer `online`, `busy`, `draining` ou `offline`. La table conserve
aussi `last_seen_at`, `max_concurrency` et la map JSON `capacity`. Les lectures
calculent `active_jobs` et le score historique. `runtime` et
`supported_protocol_version` figent la carte approuvée par le serveur; les
skills et valeurs de capacité sont rejetés s'ils ne figurent pas dans
l'allowlist de leur famille. La credential n'est jamais retournée par le
registre.

### `agent_jobs`

Une job lie une tâche, une compétence et un payload validé. Une seule ligne par
tâche peut se trouver dans `queued`, `claimed` ou `running`. La réclamation
conserve `lease_id`, le digest du token opaque, `lease_expires_at`,
`lease_generation`, `attempt_count`, `max_attempts`, le dernier agent et la
dernière raison d'échec. Le token brut n'est renvoyé qu'à la claim; la colonne
historique `claim_token` est neutralisée et conservée seulement pour migration.

La job et sa tâche transitionnent ensemble sous `BEGIN IMMEDIATE`. Une preuve
de lease périmée ou d'une ancienne génération ne peut pas écrire. Le reaper ne
remet automatiquement en file que `workspace.list_dir` et
`workspace.read_text`, dans le budget de tentatives et si la génération n'a
aucun historique de capability iPhone; les autres expirations terminent en
échec avec issue potentiellement incertaine. Une migration clôt les doublons
actifs, invalide les anciens claims en clair, préfère un survivant encore en
file et clôt toute tâche distribuée orpheline. Elle ne rejoue les deux lectures
que lorsqu'aucune exécution locale concurrente n'existe.

### `worker_skill_policy_state`

Cette ligne singleton est la projection autoritative des règles de skills
worker. Elle conserve le JSON canonique validé, son digest, un `epoch`
monotone et `updated_at`. Un reload capture l'epoch avant de parser le fichier,
puis compare-and-swap cet epoch sous `BEGIN IMMEDIATE`: un candidat devenu
périmé ne peut pas écraser une révocation plus récente. Registration, mise en
file, claim et reaper lisent cette même ligne dans la transaction de leur
mutation; un cache allow périmé ne peut pas autoriser et un cache deny périmé ne
peut pas remplacer une règle durablement permise.

### `message_board_events` et `outbox_events`

`message_board_events` est le board durable SQLite: `schema_version`,
`event_id`, topic, type, type/ID d'agrégat, tâche/agent optionnels, payload JSON,
date et `dedupe_key` unique. Les IDs produits par un backend externe ne sont
jamais l'identité métier.

`outbox_events` contient l'agrégat, le topic, le type, le payload canonique,
les identifiants corrélés, `published_at`, compteur de tentatives,
`last_error` expurgé, `event_id` et `dedupe_key` uniques. Les colonnes
`publishing_owner`, `publishing_started_at`,
`publishing_lease_expires_at` et `publish_generation` protègent plusieurs
drainers. Un `BEGIN IMMEDIATE` revendique les lignes non publiées libres ou
expirées et incrémente la génération. `mark_published` exige le propriétaire,
la génération et une lease encore valide; un publisher périmé ne peut pas
confirmer une reprise plus récente.

La transition métier et l'entrée d'outbox partagent une transaction. Le drain
publie au moins une fois; si le processus tombe après le board/Redis mais avant
`published_at`, le rejeu conserve `event_id`/`dedupe_key`, permettant au backend
de dédupliquer. Une panne de transport laisse toujours la ligne non publiée et
incrémente seulement le compteur/erreur expurgée. Aucune ligne non publiée n'est
supprimée.

`outbox_operational_metrics` et `agent_job_operational_metrics` sont deux lignes
singleton de compteurs. Elles évitent que `/status` reparcoure les historiques
d'audit/outbox append-only. Une migration absente initialise une seule fois les
compteurs worker depuis l'audit, après les réconciliations de redémarrage; les
incréments futurs partagent la transaction de la lease, du job, de l'audit et de
l'outbox.

Redis ne remplace aucune de ces lignes. Sa projection applique `MAXLEN ~` et
un trim temporel `MINID ~` seulement lors d'une nouvelle publication non
dédupliquée sur le stream concerné; ce stream porte aussi un TTL d'inactivité.
Chaque
`dedupe_key` est matérialisée séparément dans une clé Redis nommée avec son
SHA-256 et munie de son propre TTL `PX`, sans hash ni index global. La
projection peut donc perdre son historique par trim ou expiration et doit
toujours pouvoir être reconstruite depuis SQLite/API.

### `control_plane_instances` et `maintenance_leases`

Une instance reçoit un `instance_id` aléatoire propre au démarrage, un label de
hostname normalisé, une version et ses timestamps de démarrage, heartbeat et
arrêt. Le hostname seul n'est jamais l'identité.

Une lease de maintenance associe un nom, l'instance propriétaire, une
génération, acquisition/renouvellement et expiration. Le reaper de jobs,
l'expirer de capabilities, l'entretien d'outbox et le recalcul feedback/scoring
acquièrent des noms distincts. Le takeover après expiration incrémente la
génération; toute mutation singleton revérifie propriétaire/génération dans sa
transaction SQLite.

### `message_consumer_deliveries` et `message_consumer_checkpoints`

Ces tables forment le ledger des consumers internes de confiance. Une delivery
est liée au groupe, à `event_id` et `dedupe_key`; elle porte état, tentatives,
budget, propriétaire du claim, génération/expiration, erreur expurgée et issue.
Le checkpoint avance seulement après succès du handler et ack fenced. Une
delivery épuisée devient `dead_letter`; elle n'autorise aucun effet de worker.
Cette fondation est backend-neutre mais n'est pas un consumer group Redis
déployé.

### `idempotency_receipts`

Chaque reçu lie `actor_id` (device authentifié), `idempotency_key`, opération,
digest canonique et réponse JSON. L'effet ordinaire et le reçu sont écrits dans
le même `BEGIN IMMEDIATE`. Une répétition exacte retourne la réponse enregistrée;
la même clé avec une autre opération ou un autre payload est refusée. La surface
est limitée à feedback, champ `pinned` de mémoire et message de chat avec
`start_task: false`; aucune action sensible n'utilise cette table.

### `goal_runs`

Un but lie une tâche racine unique à l'objectif autoritatif, au profil
`manual`/`assisted`/`autonomous`, à la source du planner et à ses critères de
fin. Les budgets persistés couvrent pas, parallélisme, replans, durée et appels
modèle; leurs compteurs ne peuvent pas devenir négatifs. Les phases, timestamps,
résumé evaluator et fingerprints de plan/décision/état permettent une reprise
explicable et l'arrêt des boucles. Les états terminaux sont `completed`,
`failed`, `cancelled` et `budget_exhausted`; aucun n'est réouvert.

Créer un but crée sa tâche racine `planned` dans la même transaction. Cela ne
démarre ni modèle ni worker. La commande `start` valide ensuite une proposition
avant le passage à `running`.

### `plan_nodes` et `plan_edges`

Chaque nœud appartient à un but et porte type (`worker` ou `synthesis`), objectif,
skill éventuel, priorité, dépendances, sortie attendue, état, tâche/job/agent
corrélés et uniquement des résumés de résultat/erreur. Un nœud worker possède
une tâche enfant distincte; l'index unique de `task_id`/`worker_job_id` empêche
une association ambiguë. Les nœuds terminaux ne sont pas ressuscités.

`plan_edges` matérialise les dépendances `hard` ou `optional`, interdit une
auto-arête et cascade avec le but. Le validateur rejette les cycles et IDs
inconnus avant insertion. Une dépendance hard échouée bloque son descendant;
une dépendance optional terminale n'impose pas ce blocage.

### `goal_model_calls`, `goal_evaluations`, `goal_contexts` et `goal_results`

- `goal_model_calls` journalise rôle, provider, modèle éventuel, contexte,
  digests d'entrée/sortie, état et latence/erreur sans stocker un bearer. Un but
  n'accepte qu'un appel `started` à la fois et le compteur de budget est réservé
  avant le transport modèle.
- `goal_evaluations` conserve l'ordre, la décision JSON strictement validée et
  les fingerprints d'état/décision. Répéter la même décision sans changement
  d'état est détectable et arrêté.
- `goal_contexts` contient uniquement les cartes déjà expurgées/bornées, leurs
  IDs de provenance/cartes et un compte approximatif de tokens. La ligne ne
  constitue aucune permission.
- `goal_results` conserve une projection déterministe construite depuis les
  résumés SQLite. Le JSON contient comptes, nœuds sûrs et provenance; l'objet
  worker brut n'y est jamais recopié.

### `goal_feedback`

Feedback d'un but terminal: score 0–5, note, corrections optionnelles de réponse
et plan, marqueur de revue et timestamp. Les corrections sont expurgées avant
écriture. L'export peut filtrer ces lignes, mais `reviewed` et un score élevé ne
suffisent pas seuls à créer une cible: une correction humaine adaptée est aussi
requise.

### `episodes`, `episode_steps` et `episode_embeddings`

`episodes` conserve une trajectoire résumée unique par `goal_run_id`: objectif,
résumé de plan, outcome, score, durée, familles de workers, tags d'échec,
feedback utilisateur et dates. `episode_steps` ordonne des résumés d'entrée/
sortie expurgés avec type de nœud, skill, agent, état et latence optionnelle.

`episode_embeddings` est une projection optionnelle par épisode/provider avec
dimensions et vecteur JSON. Un échec d'embedding ne supprime ni ne fait échouer
l'épisode déjà autoritatif. La recherche lexicale reste disponible. Cette table
n'est pas encore projetée dans FAISS ni exposée par une API publique.

### `iphone_capability_requests`, `iphone_capability_grants` et `iphone_capability_results`

Une demande conserve tâche, job, agent, génération de lease, device ciblé,
capability, arguments canoniques, digest d'action, statut, identifiants
d'approbation/audit, `request_fingerprint` et timestamps. Le fingerprint couvre
job, génération de lease, capability et arguments canoniques; un index partiel
unique empêche deux demandes identiques simultanément non terminales. Ses états
sont `waiting_approval`,
`approved`, `consumed`, `completed`, `denied`, `failed`, `cancelled` et
`expired`.

Le grant associe exactement une demande à un device, une capability, une
approbation et un digest. Seul le hash du token opaque actuel est stocké, avec
émission, expiration et consommation. Répéter `approve` avant consommation fait
tourner ce token et invalide le précédent sans ajouter un second grant. Le
résultat terminal est unique par demande et
conserve le statut et le JSON natif validé. L'expiration de lease ou
l'annulation de la tâche annule les demandes non terminales et raccourcit tout
grant associé. Les arguments et résultats peuvent contenir des données
personnelles: ils ne passent ni dans les notifications WebSocket ni dans les
payloads d'audit ou du board, mais restent dans le fichier SQLite protégé; leur
politique de rétention opérationnelle reste à qualifier.

### `memory_items`

Scope, type, contenu, résumé, sensibilité, confiance, état épinglé, métadonnées et
timestamps. La recherche reste lexicale par défaut. Quand un provider
d'embeddings est configuré, `memory_embeddings` conserve le vecteur, son
provider et ses dimensions, et la recherche combine scores vectoriel et lexical.
Sans provider ou en cas d'échec de celui-ci, le runtime retombe sur le lexical;
la projection FAISS optionnelle est secondaire et reconstruisible. Qdrant n'est
pas annoncé comme implémenté.

### `feedback_events`

Référence optionnelle à une tâche ou un agent, type, label, score, notes, payload
et date. Le serveur valide les références présentes avant insertion.

### `agent_score_snapshots` et `scheduler_decisions`

`agent_score_snapshots` est une projection reconstruisible produite uniquement
depuis les jobs, audits de lease et feedback observés par le serveur. Elle
sépare completed/failed, expirations, taux, feedback moyen, latence et score
composite avec version de formule. La formule courante est transparente:
`0.60 × completion + 0.20 × (1 - timeout) + 0.20 × feedback_normalisé`, avec
composante neutre `0.5` lorsqu'aucune observation n'existe. La latence est
diagnostique et sert au classement seulement après trois résultats.

`scheduler_decisions` conserve la job, les candidats avec métadonnées sûres,
l'agent sélectionné, l'ordre de scoring et la date. Les tokens, endpoints,
payloads et secrets n'y figurent jamais. Même état observé et même job produisent
le même ordre de candidats; un LLM ne décide pas l'éligibilité.

### `audit_events`

Identifiant monotone, trace, type, acteur, tâche, payload, hash précédent, hash
courant et date. Chaque nouvel événement chaîne son empreinte au précédent afin
de rendre une modification ultérieure détectable; ce mécanisme n'est pas une
signature externe. La lecture API peut expurger un payload historique sensible;
les hash restent ceux de la ligne interne brute et ne sont pas recalculés sur la
projection publique.

## Projection vectorielle externe

FAISS n'est pas une table ni une source de vérité. L'adaptateur optionnel écrit
des générations contenant les IDs stables de `memory_items`, puis permute un
pointeur local `CURRENT.json` seulement après validation complète. La commande
de rebuild relit `memory_embeddings` sous un provider donné. Perte, corruption
ou absence de FAISS laisse intactes les mémoires SQLite et le fallback lexical.
Au début d'un rebuild, sous le lock de projection, les répertoires `.tmp-*` et
les pointeurs/digests temporaires privés laissés par un crash sont validés puis
supprimés. Un artefact qui n'est pas privé ou n'appartient pas à l'UID courant
fait échouer l'entretien sans être suivi ni effacé. Qdrant reste **PLANNED**.

## Outbox SQLite mobile

Dans la base Expo locale, `mutation_outbox` contient `id`, `origin`, opération,
ressource, payload JSON, clé d'idempotence, timestamps, tentatives, erreur et
fin. L'unicité `(origin,idempotency_key)` et la revalidation du contenu empêchent
une clé d'être re-liée. Les entrées ne contiennent ni bearer ni grant. Elles ne
sont drainées qu'après bootstrap autoritatif et restent isolées de toute autre
origine; un changement de jumelage les abandonne.

## Indexes actuels

- tâches par état et mise à jour;
- approbations par état et création;
- événement de demande d'approbation unique par approbation;
- appels d'outils par tâche et approbation;
- messages par conversation ou tâche;
- mémoire par scope, type, épinglage et mise à jour;
- feedback par tâche;
- audit par trace et date;
- board par topic/ordre et `dedupe_key` unique;
- board et outbox par `event_id` unique;
- outbox par publication/expiration de claim/ordre et `dedupe_key` unique;
- instances par état/heartbeat et maintenance par expiration/nom;
- consumers par groupe, état, expiration et identité/déduplication;
- reçus d'idempotence par appareil/clé et date;
- jobs par file/compétence, agent, et index partiel unique d'une job active par
  tâche;
- demandes iPhone par device/statut/date, par job/génération/date et par l'index
  partiel unique `idx_iphone_capability_one_nonterminal_fingerprint` sur le
  fingerprint tant que l'état n'est pas terminal;
- grant et résultat uniques par demande.
- snapshots de score par score/agent et décisions du scheduler par job/date.
- buts par état/date, nœuds par but/état/priorité, arêtes par but/destination,
  évaluations/appels/contextes/feedback par but et épisodes par outcome/date.

## Rétention et limites

- audit: conservation longue à définir opérationnellement;
- messages et tâches: politique de rétention encore à définir;
- mémoire: modifiable et supprimable par l'opérateur authentifié;
- feedback: conserver seulement selon la politique d'évaluation approuvée;
- codes et tickets: éphémères;
- grants iPhone: éphémères et à usage unique; arguments/résultats: rétention à
  définir selon leur sensibilité;
- sauvegarde/restauration et purge ne sont pas encore qualifiées en production.

Les migrations `0.8 → 0.12`, `0.9 → 0.12`, `0.10 → 0.12` et `0.11 → 0.12`
sont additives:
aucune table autoritative n'est recréée ou supprimée. Les colonnes anciennes
sont complétées, les anciens IDs d'événements reçoivent une identité stable et
les invariants de jobs/capabilities restent réconciliés avant la hausse de
`PRAGMA user_version`.
