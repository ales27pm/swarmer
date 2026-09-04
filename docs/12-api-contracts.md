# 12 — API Contracts

## Base URL

MVP local:

```text
http://ubuntu.local:8710
```

Prod local sécurisé:

```text
https://mongars.tailnet-name.ts.net:8710
```

## Auth

Pairing initial:

```http
POST /pairing/start
POST /pairing/confirm
```

Header ensuite:

```http
Authorization: Bearer <device_token>
X-Device-Id: iphone-ales
```

## Endpoints

### Health

```http
GET /health
```

### Bootstrap sync

```http
GET /sync/bootstrap
```

### Pull sync

```http
GET /sync/pull?cursor=<cursor>
```

### Push sync

```http
POST /sync/push
Content-Type: application/json
```

### Create task

```http
POST /tasks
```

Body:

```json
{
  "input": "Regarde le projet 27pm-crm et trouve les erreurs",
  "mode": "review",
  "source": "iphone",
  "conversation_id": "conv_..."
}
```

### Get task

```http
GET /tasks/{task_id}
```

### Cancel task

```http
POST /tasks/{task_id}/cancel
```

### Approvals

```http
GET /approvals?status=pending
POST /approvals/{approval_id}/decision
```

Decision body:

```json
{
  "decision": "allow_once",
  "user_note": "OK, seulement ce fichier",
  "scope_override": {
    "path_prefix": "/home/ales27pm/projects/27pm-crm/src/"
  }
}
```

### Memory search

```http
POST /memory/search
```

### Memory remember

```http
POST /memory/remember
```

### Agents

```http
GET /agents
GET /agents/{agent_id}
POST /agents/register
POST /agents/{agent_id}/heartbeat
```

### iPhone capability request callback

Ubuntu vers app via WebSocket. L'app répond:

```http
POST /iphone/capability-result
```

## WebSocket

```text
/ws?device_id=iphone-ales
```

Events server → iPhone:

- `task.created`
- `task.status`
- `task.message`
- `approval.requested`
- `agent.status`
- `sync.invalidate`
- `iphone.capability.request`

Events iPhone → server:

- `message.create`
- `approval.decision`
- `iphone.capability.result`
- `feedback.create`
- `device.heartbeat`

## Error shape

```json
{
  "error": {
    "code": "PERMISSION_REQUIRED",
    "message": "Action requires approval",
    "details": {},
    "trace_id": "trc_..."
  }
}
```

## OpenAPI

Voir `api/openapi.yaml` pour une version machine-readable initiale.
