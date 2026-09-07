# 14 — Message Board Events

## Objectif

Standardiser les messages entre orchestrateur, agents, gateway, state, memory, iPhone broker et feedback service.

## État du runtime `0.6`

Le serveur expose aujourd'hui un WebSocket authentifié par ticket à usage unique.
L'app mobile ne s'y abonne pas encore: ses écrans utilisent REST et un
rafraîchissement explicite. Le bus durable, Redis Streams, les consumer groups,
le dead-letter stream et l'enveloppe riche ci-dessous restent roadmap.

## Enveloppe WebSocket actuelle

Le contrat exécutable `schemas/event-envelope.schema.json` contient seulement:

```json
{
  "type": "task.updated",
  "payload": {}
}
```

Les types publiés par le serveur sont `connected`, `task.updated`,
`tool.proposed`, `tool.updated`, `tool.completed`, `tool.failed`,
`tool.execution_rejected`, `tool.outcome_uncertain`, `tool.denied`,
`approval.requested`, `approval.decided` et `orchestrator.proposed`.

## Enveloppe durable cible (roadmap)

Le futur message board durable devra suivre:

```json
{
  "id": "evt_...",
  "type": "task.status",
  "trace_id": "trc_...",
  "task_id": "tsk_...",
  "agent_id": "agent_...",
  "producer": "code-worker-01",
  "timestamp": "2026-09-04T11:30:00Z",
  "schema_version": "1.0",
  "payload": {}
}
```

## Types d'événements cibles (roadmap)

### Task

- `task.created`
- `task.planned`
- `task.assigned`
- `task.started`
- `task.progress`
- `task.blocked`
- `task.completed`
- `task.failed`
- `task.cancelled`

### Approval et exécution

- `approval.requested`
- `approval.decided`
- `approval.expired`
- `tool.denied`
- `tool.completed`
- `tool.failed`
- `execution.not_started`
- `execution.interrupted`

### Agent

- `agent.registered`
- `agent.heartbeat`
- `agent.offline`
- `agent.error`

### Memory

- `memory.search.requested`
- `memory.search.completed`
- `memory.write.candidate`
- `memory.write.accepted`
- `memory.write.rejected`

### iPhone

- `iphone.capability.requested`
- `iphone.capability.approved`
- `iphone.capability.completed`
- `iphone.capability.failed`

### Feedback

- `feedback.created`
- `feedback.scored`
- `dataset.example.created`

### Audit

- `audit.recorded`

## Clés Redis cibles (roadmap)

```yaml
streams:
  tasks_inbox: tasks.inbox
  tasks_status: tasks.status
  permission_requests: permission.requests
  permission_decisions: permission.decisions
  agents_heartbeat: agents.heartbeat
  memory_events: memory.events
  iphone_requests: iphone.requests
  iphone_results: iphone.results
  feedback_events: feedback.events
  audit_events: audit.events
```

## Consumer groups cibles (roadmap)

```yaml
consumer_groups:
  orchestrator: cg.orchestrator
  workers: cg.workers
  mobile_push: cg.mobile_push
  feedback: cg.feedback
  audit: cg.audit
```

## Idempotency cible (roadmap)

Chaque consumer doit stocker les event ids traités. Un event répété ne doit pas causer une double action.

## Ordering cible (roadmap)

- `task_id` conserve l'ordre logique via `created_at` + `seq`.
- Ne pas dépendre de l'ordre global de tous les streams.
- Les décisions permission doivent inclure approval id et action hash.

## Dead letter cible (roadmap)

Events invalides vont dans:

```text
errors.deadletter
```

Payload:

```json
{
  "original_event": {},
  "error": "schema_validation_failed",
  "consumer": "code-worker-01"
}
```
