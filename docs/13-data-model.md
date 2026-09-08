# 13 — Data Model

## Autorité et version

Le schéma implémenté est SQLite WAL, version `10`, dans
`server/app/services/state_service.py`. Il est migré de façon additive. Postgres,
Redis et le câblage de base de données du prototype Vibecode ne sont pas branchés
au MVP.

La version 7 a ajouté `message_board_events`, `agent_jobs`,
`memory_embeddings`, `eval_examples`, `corrections` et `agent_scores`. Les
versions 8–10 ajoutent l'outbox transactionnelle, les leases/générations,
l'état de capacité des agents, le transport de capabilities iPhone et la
déduplication de création par fingerprint. Il ne s'agit pas d'un branchement
Redis/NATS: board, outbox et état partagent SQLite.

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
- la première authentification ordinaire du candidat finalisé remplace le jeton
  actif dans la même transaction et supprime ses tickets WebSocket inutilisés;
- `websocket_tickets.ticket_hash` contient le digest d'un ticket court,
  consommable une fois.

### `agents`

Nom, version, endpoint, modèle, skills, état, heartbeat et digest de la
credential dédiée. Un agent débute `unverified`; un heartbeat authentifié peut
ensuite le déclarer `online`, `busy`, `draining` ou `offline`. La table conserve
aussi `last_seen_at`, `max_concurrency` et la map JSON `capacity`. Les lectures
calculent `active_jobs` et le score historique; la credential n'est jamais
retournée par le registre.

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

### `message_board_events` et `outbox_events`

`message_board_events` est le board durable SQLite: topic, type, identifiant du
message, tâche/agent optionnels, payload JSON, date et `dedupe_key` unique.

`outbox_events` contient l'agrégat, le topic, le type, le payload canonique,
les identifiants corrélés, `published_at`, compteur de tentatives,
`last_error` expurgé et `dedupe_key` unique. La transition métier et l'entrée
d'outbox partagent une transaction. Le drain publie au moins une fois; si le
processus tombe après l'insertion dans le board mais avant `published_at`, le
rejeu retrouve la même ligne grâce à `dedupe_key` au lieu de créer un doublon.
Ce mécanisme ne fournit pas de consumer groups ou de transport multi-hôte.

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
aucun FAISS/Qdrant externe n'est annoncé.

### `feedback_events`

Référence optionnelle à une tâche ou un agent, type, label, score, notes, payload
et date. Le serveur valide les références présentes avant insertion.

### `audit_events`

Identifiant monotone, trace, type, acteur, tâche, payload, hash précédent, hash
courant et date. Chaque nouvel événement chaîne son empreinte au précédent afin
de rendre une modification ultérieure détectable; ce mécanisme n'est pas une
signature externe. La lecture API peut expurger un payload historique sensible;
les hash restent ceux de la ligne interne brute et ne sont pas recalculés sur la
projection publique.

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
- outbox par publication/ordre et `dedupe_key` unique;
- jobs par file/compétence, agent, et index partiel unique d'une job active par
  tâche;
- demandes iPhone par device/statut/date, par job/génération/date et par l'index
  partiel unique `idx_iphone_capability_one_nonterminal_fingerprint` sur le
  fingerprint tant que l'état n'est pas terminal;
- grant et résultat uniques par demande.

## Rétention et limites

- audit: conservation longue à définir opérationnellement;
- messages et tâches: politique de rétention encore à définir;
- mémoire: modifiable et supprimable par l'opérateur authentifié;
- feedback: conserver seulement selon la politique d'évaluation approuvée;
- codes et tickets: éphémères;
- grants iPhone: éphémères et à usage unique; arguments/résultats: rétention à
  définir selon leur sensibilité;
- sauvegarde/restauration et purge ne sont pas encore qualifiées en production.
