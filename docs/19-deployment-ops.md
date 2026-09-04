# 19 — Deployment and Operations

## Objectif

Déployer localement sans dépendance cloud obligatoire.

## Environnements

### dev

- Expo dev server;
- FastAPI reload;
- SQLite;
- Redis local;
- llama.cpp local;
- logs verbose.

### local-prod

- Expo development/production build;
- FastAPI via systemd;
- SQLite/Postgres;
- Redis service;
- llama.cpp systemd;
- logs rotated;
- Tailscale.

### distributed

- Ubuntu main control plane;
- workers sur machines distantes;
- NATS/Redis central;
- per-worker tokens.

## systemd services

Services à créer:

- `mongars-api.service`
- `mongars-llm.service`
- `mongars-worker-code.service`
- `mongars-worker-files.service`
- `mongars-redis.service` via package

## Health checks

```http
GET /health
GET /agents
GET /sync/bootstrap
```

LLM:

```http
GET http://127.0.0.1:8711/health
```

Redis:

```bash
redis-cli ping
```

## Backups

Daily:

```bash
sqlite3 data/mongars.db ".backup data/backups/mongars-$(date +%F).db"
```

Artifacts:

```bash
rsync -a data/artifacts/ data/backups/artifacts/
```

Vector index:

```bash
tar -czf data/backups/vector-index-$(date +%F).tar.gz data/vector/
```

## Logs

- app logs: structured JSON;
- audit logs: append-only;
- worker logs: per-agent;
- mobile debug export: user-triggered.

## Update procedure

1. Stop workers.
2. Backup DB/vector/artifacts.
3. Pull/update code.
4. Run local checks.
5. Run migrations.
6. Start API.
7. Start workers.
8. Smoke test iPhone pairing/task/approval.

## Rollback

- keep previous model manifest;
- keep previous prompts;
- DB backup before migrations;
- version configs;
- audit remains append-only.

## Observability dashboard later

- task queue depth;
- agent heartbeat;
- approval pending count;
- LLM invalid JSON rate;
- memory retrieval hit rate;
- feedback score trend.
