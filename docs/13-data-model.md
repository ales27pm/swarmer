# 13 — Data Model

## Autorité et version

Le schéma implémenté est SQLite WAL, version `5`, dans
`server/app/services/state_service.py`. Il est migré de façon additive. Postgres,
Redis et le câblage de base de données du prototype Vibecode ne sont pas branchés
au MVP.

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
ensuite le déclarer `online`, `busy` ou `offline`. La credential n'est jamais
retournée par les lectures du registre.

### `memory_items`

Scope, type, contenu, résumé, sensibilité, confiance, état épinglé, métadonnées et
timestamps. La recherche `0.6.0` est lexicale, sans colonne d'embedding ni
prétention sémantique.

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
- audit par trace et date.

## Rétention et limites

- audit: conservation longue à définir opérationnellement;
- messages et tâches: politique de rétention encore à définir;
- mémoire: modifiable et supprimable par l'opérateur authentifié;
- feedback: conserver seulement selon la politique d'évaluation approuvée;
- codes et tickets: éphémères;
- sauvegarde/restauration et purge ne sont pas encore qualifiées en production.
