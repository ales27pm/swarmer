# 14 — Message Board Events

## Objectif

Standardiser les événements internes entre orchestrateur, workers, gateway,
state, capability broker et UI sans confondre le board SQLite avec le canal
WebSocket mobile.

## IMPLEMENTED — runtime `0.9.0`

Le runtime possède deux mécanismes distincts:

1. `outbox_events` et `message_board_events`, durables dans la base SQLite
   autoritative;
2. `/ws`, canal best-effort authentifié par ticket unique pour rafraîchir l'UI.

Redis Streams, NATS, consumer groups et une dead-letter queue externe restent
**PLANNED**.

## Outbox transactionnelle

Une transition de job, tâche ou capability insère son audit et son entrée
`outbox_events` dans le même `BEGIN IMMEDIATE`. Le commit de l'état ne dépend
donc pas de la disponibilité momentanée du board.

Le drain lit les entrées sans `published_at` par identifiant croissant, publie
leur payload dans `message_board_events`, puis marque l'outbox. La livraison est
au moins une fois. `dedupe_key`, unique dans les deux tables, rend sûr un crash
après publication mais avant marquage: le rejeu récupère l'événement existant.
Un échec incrémente `attempts` et conserve seulement une classe d'erreur
expurgée. Le drain s'exécute au démarrage, après les transitions et dans la
boucle de maintenance.

Ce mécanisme garantit la persistance locale et la déduplication; il ne garantit
ni diffusion réseau, ni consumer group, ni ordre global inter-topic.

## Enveloppe du board SQLite

Une ligne matérialisée contient:

```json
{
  "id": 42,
  "topic": "tasks.status",
  "event_type": "lease_expired",
  "message_id": "job_...",
  "agent_id": "agt_...",
  "task_id": "tsk_...",
  "payload": {"job_id": "job_...", "lease_generation": 2},
  "created_at": "2026-09-08T13:00:00Z",
  "dedupe_key": "agent-job:job_...:lease-expired:2"
}
```

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

Le board est une interface remplaçable, mais l'unique implémentation livrée est
SQLite. Les workers actuels utilisent l'API HTTP authentifiée pour claim,
heartbeat, résultat et poll; ils ne consomment pas un stream Redis caché.

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

`GET /status`, authentifié comme les autres ressources, expose les compteurs
`queued_jobs`, `leased_jobs`, `dead_letter_jobs`, `expired_leases`, `retries`,
`dead_letter_events`, `pending_outbox_events` et
`pending_capability_requests`. Ces compteurs décrivent l'état SQLite local; ils
ne prouvent ni livraison réseau ni disponibilité d'un bus externe.

## PLANNED — bus externe

Une migration vers Redis Streams ou NATS JetStream devra définir explicitement:

- consumer groups et acknowledgements;
- partitionnement/ordre par agrégat;
- propagation inter-hôtes et reprise;
- dead-letter queue externe et politique opérateur;
- idempotence durable des consommateurs hors SQLite;
- observabilité et limites de rétention.

Aucun nom de stream futur ne doit être présenté comme une dépendance active du
runtime `0.9.0`.
