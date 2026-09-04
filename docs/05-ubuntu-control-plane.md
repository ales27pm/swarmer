# 05 — Ubuntu Control Plane

## Objectif

Ubuntu héberge le cerveau durable:

- source de vérité du state;
- modèles locaux;
- gateway;
- workers;
- mémoire;
- bus de messages;
- logs/audit;
- feedback.

## Services

```text
services/
  api-gateway/            FastAPI REST + WebSocket
  orchestrator/           prompt + parser + model client
  permission-gateway/     risk + approvals + policy
  state-service/          DB access only
  memory-service/         embeddings + search
  feedback-service/       event scoring + dataset builder
  agent-registry/         agent cards + heartbeat
  worker-runtime/         local workers
  iphone-broker/          phone capability routing
  audit-ledger/           append-only logs
```

## Ports suggérés

| Service | Port | Scope |
|---|---:|---|
| API Gateway | 8710 | LAN/VPN seulement |
| LLM llama.cpp server | 8711 | localhost par défaut |
| Redis | 6379 | localhost/LAN restreint |
| Qdrant optionnel | 6333 | localhost/LAN restreint |
| Worker local | 8720+ | localhost/VPN |

## API Gateway

Responsabilités:

- `/pairing/*`
- `/tasks/*`
- `/approvals/*`
- `/memory/*`
- `/agents/*`
- `/sync/*`
- `/ws`
- `/health`

## Orchestrator service

Entrée:

- user intent;
- contexte conversation;
- retrieved memory;
- available agents;
- permission policy summary;
- device capabilities.

Sortie stricte:

```json
{
  "type": "orchestrator_decision",
  "task_id": "tsk_...",
  "intent_summary": "...",
  "plan": [{"step": 1, "agent": "code-worker", "action": "inspect"}],
  "tool_calls": [],
  "permission_requests": [],
  "user_message": "..."
}
```

## LLM serving

Options:

- llama.cpp server pour GGUF Q4;
- Ollama pour simplicité;
- vLLM/SGLang si GPU/server plus robuste plus tard.

MVP recommandé:

```bash
llama-server   -hf mradermacher/Hermes-3-Llama-3.2-3B-abliterated-GGUF:Q4_K_M   --host 127.0.0.1   --port 8711   -c 8192
```

## State Service

Le seul service autorisé à écrire dans DB.

Méthodes:

- `create_task`
- `update_task_status`
- `append_message`
- `create_approval`
- `resolve_approval`
- `upsert_agent`
- `record_sync_op`
- `record_artifact`
- `record_feedback`

## Memory Service

Méthodes:

- `remember`
- `search`
- `pin`
- `forget`
- `summarize_thread`
- `build_context_pack`

## Permission Gateway

Méthodes:

- `evaluate_action`
- `request_approval`
- `resolve_approval`
- `normalize_refusal`
- `execute_if_allowed`

## Agent Registry

Publie les cartes d'agents locales:

```http
GET /agents
GET /agents/{id}
POST /agents/register
POST /agents/{id}/heartbeat
```

## Workers

Chaque worker expose:

- `/health`
- `/.well-known/agent-card.json`
- `/invoke`
- `/events`

## Sécurité réseau

MVP LAN:

- bind à IP locale ou Tailscale;
- token pairing;
- allowlist devices;
- jamais exposer à Internet ouvert.

Plus tard:

- mTLS;
- WireGuard/Tailscale;
- per-agent JWT;
- certificate pinning côté app.
