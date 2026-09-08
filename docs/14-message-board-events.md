# 14 — Message Board Events

## Objectif

Standardiser les événements internes entre orchestrateur, workers, gateway,
state, capability broker et UI sans confondre le board SQLite avec le canal
WebSocket mobile.

## IMPLEMENTED — runtime `0.10.0`

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

Si Redis est absent ou tombe, l'outbox reste autoritative et non publiée; les
transitions de tâche/job déjà commises ne sont ni annulées ni perdues. La santé
passe à `degraded`, sans URL ni credential. Le prochain drain reprend la ligne.
Un ID ou ack Redis ne modifie jamais directement le state SQLite.

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

## Leases, retry et dead letter locaux

Une claim crée un token opaque conservé seulement sous forme de hash, un
`lease_id`, une date d'expiration et une génération. Le heartbeat renouvelle la
date. Une preuve ancienne ou étrangère reçoit `409`.

Le reaper émet `lease_expired`, puis:

- remet en file uniquement `workspace.list_dir` ou `workspace.read_text` si le
  budget de tentatives reste disponible et si la génération n'a aucune activité
  de capability iPhone;
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

Tous les WebSocket passent par un sérialiseur central. Les événements partagés
refusent les champs bearer/credential/secret/token/grant, arguments ou résultats
natifs, URL contenant des identifiants et texte ressemblant à un bearer. Les
résultats d'outil sont projetés sous forme expurgée; une mise à jour mémoire ne
porte que ses métadonnées sûres. Le même contrat de payload est appliqué avant
toute insertion dans l'outbox et donc avant SQLite ou Redis.

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
- backend de board, santé et dernière publication réussie;
- `outbox_pending`, `outbox_publishing`, `outbox_failed`;
- `queued_jobs`, `leased_jobs`, `dead_letter_jobs`, `expired_leases`, `retries`
  et `dead_letter_events`;
- agents actifs/hors ligne, propriétaires des leases de maintenance, demandes
  iPhone en attente et backend vectoriel configuré.

Ces compteurs/labels ne contiennent aucun payload ni credential. Ils décrivent
l'état local et la santé vue par l'instance; ils ne constituent pas une preuve
de disponibilité multi-hôte.

## PLANNED — exploitation multi-hôte

Une qualification de Redis Streams ou une migration vers NATS JetStream devra
encore définir explicitement:

- consumer groups et acknowledgements;
- partitionnement/ordre par agrégat;
- propagation inter-hôtes et reprise;
- dead-letter queue externe et politique opérateur;
- idempotence durable des consommateurs hors SQLite;
- observabilité et limites de rétention.

Le backend SQLite reste le défaut. L'existence de l'adaptateur Redis ne constitue
pas une revendication de production multi-hôte.
