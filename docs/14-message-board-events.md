# 14 — Message Board Events

## Objectif

Standardiser les messages entre orchestrateur, agents, gateway, state, memory, iPhone broker et feedback service.

## Event envelope

Tous les événements suivent:

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

## Core event types

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

### Permission

- `permission.requested`
- `permission.decided`
- `permission.expired`
- `permission.denied`

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

## Redis stream keys

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

## Consumer groups

```yaml
consumer_groups:
  orchestrator: cg.orchestrator
  workers: cg.workers
  mobile_push: cg.mobile_push
  feedback: cg.feedback
  audit: cg.audit
```

## Idempotency

Chaque consumer doit stocker les event ids traités. Un event répété ne doit pas causer une double action.

## Ordering

- `task_id` conserve l'ordre logique via `created_at` + `seq`.
- Ne pas dépendre de l'ordre global de tous les streams.
- Les décisions permission doivent inclure approval id et action hash.

## Dead letter

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
