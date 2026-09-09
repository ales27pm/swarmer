# 14 — Message Board Events

## Objectif

Standardiser les événements internes entre orchestrateur, workers, gateway,
state, capability broker et UI sans confondre le board SQLite avec le canal
WebSocket mobile.

## IMPLEMENTED — runtime `0.11.0`

Le runtime possède deux mécanismes distincts:

1. `outbox_events`, durable dans la base SQLite autoritative, puis un backend de
   notification SQLite par défaut ou Redis Streams optionnel;
2. `/ws`, canal best-effort authentifié par ticket unique pour rafraîchir l'UI.

Redis Streams est **IMPLEMENTED** comme transport optionnel et non autoritatif.
NATS, un pipeline consumer Redis de production et une dead-letter queue externe
restent **PLANNED**.

## Outbox transactionnelle

Une transition de job, tâche ou capability insère son audit et son entrée
`outbox_events` dans le même `BEGIN IMMEDIATE`. Le commit de l'état ne dépend
donc pas de la disponibilité momentanée du board.

Un drainer identifié par son `instance_id` revendique atomiquement les entrées
sans `published_at` qui sont libres ou dont la lease de publication a expiré.
Le claim écrit `publishing_owner`, dates de début/expiration et incrémente
`publish_generation`. Après publication, `mark_published` exige encore ce
propriétaire, cette génération et une lease non expirée. Un publisher ancien ne
peut pas confirmer une nouvelle génération.

La livraison est au moins une fois. `event_id` et `dedupe_key` applicatifs sont
stables et uniques, ce qui rend sûr un crash après publication mais avant
marquage: le rejeu republie la même identité et le backend retourne un ack de
duplicate. Un échec libère le claim, incrémente `attempts` et conserve seulement
une classe d'erreur expurgée. Les claims expirés sont récupérés au démarrage et
par la boucle singleton `outbox-maintenance`; une ligne non publiée n'est jamais
supprimée.

Ce mécanisme garantit la persistance autoritative locale et la convergence
après crash. Redis apporte une diffusion réseau de notification, mais cette
release ne garantit ni déploiement multi-hôte prêt production, ni ordre global
inter-topic.

### Publication plus longue que la lease — QUALIFIED

Si un appel broker dépasse la lease de publication, Redis peut accepter la
génération N puis le `mark_published` local de N est refusé parce qu'il est
stale. Une génération N+1 republie alors le même `event_id` et la même
`dedupe_key`. Ce comportement est volontairement **at-least-once**; la
déduplication applicative rend le duplicate sans effet métier. Les compteurs
`outbox_duplicate_publications`, `outbox_claim_expirations` et les agrégats
`outbox_publish_latency_ms_*` l'exposent. Aucune garantie exactly-once n'est
revendiquée.

## Enveloppe durable backend-neutre

Une ligne matérialisée contient:

```json
{
  "schema_version": "1.0",
  "event_id": "evt_01",
  "dedupe_key": "agent-job:job_01:lease-expired:2",
  "topic": "tasks.status",
  "event_type": "lease_expired",
  "aggregate_type": "agent_job",
  "aggregate_id": "job_01",
  "agent_id": "agt_...",
  "task_id": "tsk_...",
  "payload": {"job_id": "job_...", "lease_generation": 2},
  "created_at": "2026-09-08T13:00:00Z"
}
```

L'ID de ligne SQLite ou l'ID de stream Redis est seulement un curseur backend;
il ne remplace jamais `event_id` ou `dedupe_key` pour l'idempotence métier.

Types acceptés par l'implémentation:

- `published`, `claimed`, `heartbeat`, `acked`, `failed`, `cancelled`;
- `lease_expired`, `dead_lettered`;
- `capability_requested`, `capability_authorized`,
  `capability_consumed`, `capability_result`.

Topics produits dans le slice:

| Topic | Événements principaux |
|---|---|
| `tasks.inbox` | publication, claim et remise en file d'une job |
| `tasks.status` | résultat, annulation, expiration de lease, dead letter |
| `agents.heartbeat` | heartbeat d'une lease de job |
| `iphone.capabilities` | demande, autorisation et consommation |
| `agent.job.capability.result` | résultat, expiration ou annulation vers le worker |

Pour un résultat iPhone, le board ne conserve que `request_id` et `status`. La
valeur native validée reste dans la table de résultats SQLite et n'est renvoyée
qu'au worker autorisé par le poll sous lease.

Les workers actuels utilisent l'API HTTP authentifiée pour claim, heartbeat,
résultat et poll; ils ne consomment pas un stream Redis caché et ne reçoivent
aucun credential du broker.

## Backends de notification

### SQLite — défaut

`SQLiteMessageBoard` matérialise l'enveloppe dans `message_board_events`. Sa
contrainte `dedupe_key` rend les publications répétées observables comme
duplicates sans ajouter de seconde ligne.

### Redis Streams — optionnel

`RedisStreamsMessageBoard` est activé par
`MONGARS_MESSAGE_BOARD_BACKEND=redis`; la connexion vient de
`MONGARS_REDIS_URL` et les streams de
`MONGARS_REDIS_STREAM_PREFIX`. Les topics sont regroupés en quatre streams:
`<prefix>:tasks`, `:agents`, `:iphone` et `:system`. Un script Redis associe
atomiquement la `dedupe_key` applicative à l'ID du stream avant de répondre.
Cette liaison utilise une clé Redis indépendante nommée avec le SHA-256 de la
`dedupe_key`, pas un index global.

`MONGARS_REDIS_STREAM_MAXLEN` borne approximativement le nombre d'entrées;
`MONGARS_REDIS_STREAM_RETENTION_SECONDS` fournit la fenêtre temporelle. À chaque
nouvelle publication non dédupliquée, le script applique `MAXLEN ~` puis
`MINID ~` au stream ciblé et renouvelle le TTL d'inactivité de ce stream. La
liaison de déduplication reçoit son propre TTL `PX`; aucun hash ou index global
n'est conservé. Le stream et les clés de déduplication expirent selon leurs TTL
renouvelés indépendamment. Le trim ou cette
expiration ne supprime jamais l'état métier SQLite; un consumer devenu trop
ancien doit reconstruire sa projection depuis l'API/base autoritative et ne
peut jamais supposer un historique Redis infini.

Si Redis est absent ou tombe, l'outbox reste autoritative et non publiée; les
transitions de tâche/job déjà commises ne sont ni annulées ni perdues. La santé
passe à `degraded`, sans URL ni credential. Le prochain drain reprend la ligne.
Un ID ou ack Redis ne modifie jamais directement le state SQLite.

### Qualification Redis réelle

**QUALIFIED:** huit tests live ont exercé un Redis authentifié sur `ubuntu-host`
via tunnel SSH, dont panne/reprise, backlog, duplicate applicatif et rétention.
Le point d'entrée est `scripts/test-redis-integration.sh`.

**EXPERIMENTAL / NOT QUALIFIED:** TLS Redis et le chemin de certificat invalide
n'ont pas été exécutés dans ce harness. L'adaptateur impose toujours TLS hors
loopback, mais cette validation de configuration n'est pas une preuve de test
TLS live.

## Consumers de confiance — fondation implémentée

`ConsumerCheckpointStore` et `MessageConsumer` fournissent un ledger SQLite aux
services internes du control plane. L'identité de groupe/consumer est contrôlée
par l'application. Une delivery est claimed avec expiration et génération;
l'ack et le checkpoint n'avancent qu'après succès du handler et avec la preuve
de claim courante. Les échecs ont un retry borné, puis `dead_letter`; un handler
reçoit une clé d'idempotence stable.

Cette fondation émule les sémantiques attendues en tests SQLite. Elle n'autorise
pas les workers à consommer Redis et n'est pas encore raccordée à un consumer
group Redis opérationnel.

## Maintenance continuellement fencée — IMPLEMENTED

Les opérations singleton utilisent `MaintenanceLeaseRunner`: acquisition d'une
lease nommée, renouvellement avant 50 % du TTL et génération transmise au code
de mutation. Chaque batch autoritatif appelle `require_current_locked()` dans
la même transaction SQLite. Une perte de renouvellement annule le travail; un
ancien owner ne peut pas continuer après qu'une génération N+1 a pris la lease.
Ce contrat couvre reaper de jobs, expiration des capabilities, récupération de
l'outbox et calcul des scores. La reconstruction FAISS reste une commande
manuelle, pas une boucle automatique cachée.

## Leases, retry et dead letter locaux

Une claim crée un token opaque conservé seulement sous forme de hash, un
`lease_id`, une date d'expiration et une génération. Le heartbeat renouvelle la
date. Une preuve ancienne ou étrangère reçoit `409`.

Le reaper émet `lease_expired`, puis:

- remet en file uniquement `workspace.list_dir` ou `workspace.read_text` si le
  budget de tentatives reste disponible et si la génération n'a aucune activité
  de capability iPhone;
- quarantine une job dont le skill a été révoqué; une lease encore valide peut
  finir, mais son expiration ne peut jamais provoquer une redistribution;
- annule si la tâche parente est déjà terminale;
- sinon échoue la job/tâche, qualifie l'issue d'incertaine lorsque nécessaire et
  écrit `dead_lettered` dans `tasks.status`.

Ce dernier événement est une preuve locale observable, pas une file externe à
rejouer automatiquement.

Le renouvellement de lease reste enregistré à chaque heartbeat dans
`agent_jobs`. L'événement durable `agents.heartbeat` est en revanche dédupliqué
par job et génération: la fréquence de renouvellement ne fait pas croître
indéfiniment l'outbox et le board.

## WebSocket mobile best-effort

Le contrat exécutable `schemas/event-envelope.schema.json` garde l'enveloppe
minimale:

```json
{"type": "task.updated", "payload": {}}
```

Les types actuellement émis sont:

- `connected`, `task.updated`, `message.created`, `orchestrator.proposed`;
- `tool.proposed`, `tool.updated`, `tool.completed`, `tool.failed`,
  `tool.execution_rejected`, `tool.outcome_uncertain`, `tool.denied`;
- `approval.requested`, `approval.decided`;
- `agent.job.queued`, `agent.job.claimed`, `agent.job.completed`,
  `agent.job.failed`;
- `iphone.capability.requested`, `iphone.capability.updated`.

Les événements iPhone sont envoyés seulement au device ciblé.
`iphone.capability.requested` transporte `request_id`, `capability_name`,
`expires_at` et `preview.arguments_redacted: true`;
`iphone.capability.updated` transporte uniquement `request_id`. Aucun argument,
résultat natif, bearer ou grant n'entre dans la notification. Après reconnexion,
le client fait un bootstrap ou un `GET` REST autoritatif; le WebSocket n'est ni
un journal complet, ni une preuve de succès.

`task.*`, `message.*` et `approval.*` sont eux aussi des notifications
d'invalidation, pas des transports de contenu. Une tâche ne publie que ses
identifiants, son statut et ses horodatages; un message ne publie que ses
identifiants, son rôle et ses horodatages; une approbation ne publie que ses
identifiants, son statut et ses échéances. Le marqueur `refetch_required: true`
oblige le client à relire le contenu autoritatif par REST. `title`, `input`,
`content`, `user_note`, snapshots d'action, métadonnées libres et erreurs
textuelles ne passent jamais dans ces événements. La réplica mobile ne remplace
pas une ligne complète par cette projection volontairement partielle. Le
provider regroupe les rafales, n'exécute jamais deux réconciliations en
parallèle, effectue au plus trois reprises différées bornées et clôt toute passe
devenue obsolète après un changement d'origine.
`orchestrator.proposed` suit le même contrat: `task_id`, `planner_source`, état
éventuel et `refetch_required`, sans résumé du modèle, arguments ou nom d'outil.
La proposition détaillée reste accessible seulement dans la réponse REST
authentifiée et l'état autoritatif associé à la tâche.

Le ticket est créé sous writer lock après revalidation du bearer actif et de la
lignée `last_pairing_id`. Un re-pair supprime les tickets non consommés et rend
obsolète la lignée mémorisée par tout WebSocket déjà ouvert; les chemins d'envoi
et de réception revérifient cette lignée et ferment la session ancienne avant
de transporter un nouvel événement. `devices.websocket_connection_id` clôt
également les connexions concurrentes entre processus: seule la connexion
durablement courante peut recevoir. Les envois et fermetures sont bornés par
`MONGARS_WEBSOCKET_IO_TIMEOUT_SECONDS`, puis la socket fautive est évincée hors
du verrou de bascule. Le fan-out par appareil est concurrent: plusieurs pairs
lents coûtent un seul intervalle de timeout, pas leur somme.

Les processus se relaient ces projections via `websocket_notifications` dans la
SQLite autoritative, indépendamment du choix SQLite/Redis pour le message board.
Chaque instance avance son propre checkpoint après la livraison; un crash entre
l'envoi et le checkpoint peut donc produire un duplicate inoffensif. Le journal
est nettoyé seulement jusqu'au minimum des checkpoints encore vivants. Une
instance revenue après expiration de son checkpoint reçoit `sync.invalidated`
et force un bootstrap REST. L'insertion WebSocket reste une indication
best-effort post-commit: une panne à cet endroit ne change jamais la réponse ni
l'état métier, et reconnexion/bootstrap reste le mécanisme de convergence.

Tous les WebSocket passent par un sérialiseur central. Les événements partagés
refusent les champs bearer/credential/secret/token/grant, arguments ou résultats
natifs, URL contenant des identifiants et texte ressemblant à un bearer. Les
résultats d'outil sont projetés sous forme expurgée; une mise à jour mémoire ne
porte que ses métadonnées sûres. Le même contrat de payload est appliqué avant
toute insertion dans l'outbox et donc avant SQLite ou Redis.
Les payloads durables refusent en plus les champs de texte libre usuels
(`content`, `input`, `prompt`, `title`, `description`, `summary`, `note`): le
board transporte des identifiants, états et catégories machine; le détail reste
dans SQLite et ses API authentifiées.

En `0.11.0`, des tests paramétrés couvrent aussi les objets/tableaux imbriqués,
variantes de casse, camelCase, tirets, séparateurs invisibles et homoglyphes
Unicode courants pour les alias de secrets, corps mail/SMS, contacts et
coordonnées. Cette défense complète la minimisation par type d'événement; elle
ne remplace pas la règle de ne jamais placer la donnée sensible dans un payload
partagé.

## Ordering et idempotence

- L'identifiant SQLite fournit l'ordre de matérialisation local du board.
- L'ordre métier d'une job repose sur son état et sa `lease_generation`, pas sur
  un ordre global de tous les topics.
- Les transitions terminales sont conditionnelles et les résultats de job ou
  de capability n'acceptent qu'un rejeu strictement identique.
- Une demande capability identique sous la même job/génération est dédupliquée
  avant publication; des arguments différents créent une demande distincte.
- Une reprise `approve` avant consommation fait tourner le grant non consommé;
  le secret remplacé ne peut plus être utilisé.
- Les décisions sensibles restent liées à l'identifiant d'approbation et au
  digest canonique de l'action.

## Observabilité locale

`GET /status`, authentifié comme les autres ressources, expose seulement:

- version et `instance_id` de boot;
- backend de board, santé, dernière publication, nombre de reconnects Redis et
  catégorie d'erreur expurgée;
- `outbox_pending`, `outbox_publishing`, `outbox_failed`, expirations de claim,
  duplicates et agrégats de latence;
- `queued_jobs`, `leased_jobs`, `dead_letter_jobs`, `quarantined_jobs`,
  `expired_leases`, `retries` et `dead_letter_events`;
- agents actifs/hors ligne, propriétaires des leases de maintenance, demandes
  iPhone en attente, échecs de renouvellement, backend vectoriel et âge de sa
  génération active.

Les cumuls d'expirations/duplicates/latence outbox et
leases-expirées/retries/dead-letters worker sont persistés en lignes singleton
SQLite et avancent avec les transactions qui produisent les événements. Leur
lecture est bornée et ne reparcourt ni l'outbox publiée ni l'audit append-only.
Ces compteurs/labels ne contiennent aucun payload ni credential. Ils décrivent
l'état local et la santé vue par l'instance; ils ne constituent pas une preuve
de disponibilité multi-hôte.

## Frontière multi-hôte

**SUPPORTED:** un seul control plane Ubuntu avec SQLite autoritatif local,
plusieurs workers distants via API authentifiée et Redis optionnel comme fabric
de notification.

**QUALIFIED (protocole/processus):** `scripts/run-multihost-smoke.sh` exerce deux
identités worker, failover de lease, fencing d'un worker revenu tardivement et
panne de Redis sans perte d'état. Deux machines worker physiques distinctes
restent **EXPERIMENTAL / NOT RUN**.

**UNSUPPORTED:** plusieurs control planes qui écrivent le même fichier SQLite
sur NFS/filesystem réseau et tout active-active SQLite inter-hôtes.

## PLANNED — exploitation étendue

Une qualification de Redis Streams ou une migration vers NATS JetStream devra
encore définir explicitement:

- consumer groups et acknowledgements;
- partitionnement/ordre par agrégat;
- qualification de workers sur hôtes physiques distincts et reprise;
- dead-letter queue externe et politique opérateur;
- idempotence durable des consommateurs hors SQLite;
- observabilité multi-instance externe.

Le backend SQLite reste le défaut. La qualification Redis ne transforme pas le
broker en source de vérité et ne prouve pas une architecture active-active.
