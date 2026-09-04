# monGARS Swarm App

Console iPhone local-first qui pilote un control plane d'agents IA — construite d'après les spécifications du repo [`swarmer/`](./swarmer) (MASTER_SPEC + docs 01–25).

## Principe central

> Modèles = pensée et délégation. Gateway = permission, risque, audit. Executors = actions sandboxées.

Aucune action sensible ne s'exécute sans approbation humaine. Le backend garde la vérité officielle (state, mémoire, audit log, registry d'agents).

## Structure

```text
mobile/    App Expo — console du swarm (React Native, Expo Router, React Query, NativeWind)
backend/   Control plane Hono + SQLite (WAL) — state service, orchestrateur simulé, permission gateway
swarmer/   Pack de documentation source (specs, ADRs, contrats API)
```

## Écrans (mobile)

- **Console** — chat avec l'orchestrateur; chaque message crée une tâche.
- **Tâches** — cycle de vie complet: en file → en cours → permission → terminée, avec filtres.
- **Approbations** — Permission Gateway: qui demande, quoi, pourquoi, risque, diff. Autoriser / Refuser.
- **Mémoire** — mémoire longue durée: recherche, épinglage, ajout, suppression.
- **Agents** — registry: statut, modèle, skills, heartbeat.
- **Réglages** — appairage d'appareil (code à 6 chiffres), statistiques du control plane, journal d'audit hash-chaîné.
- **Détail tâche** — timeline des messages, approbation inline, annulation, feedback (enregistré pour evals futures).

## API (backend, préfixe `/api`, enveloppe `{ data }`)

| Endpoint | Description |
| --- | --- |
| `POST /api/pairing/start` · `POST /api/pairing/confirm` | Appairage d'appareil |
| `POST /api/chat` | Message → conversation + tâche orchestrée |
| `GET/POST /api/tasks` · `GET /api/tasks/:id` · `POST /api/tasks/:id/cancel` | Cycle de vie des tâches |
| `GET /api/approvals` · `POST /api/approvals/:id/decision` | Permission Gateway (`allow_once` / `allow_rule` / `deny`) |
| `GET /api/memory` · `POST /api/memory/search` · `POST /api/memory/remember` · `PATCH/DELETE /api/memory/:id` | Memory Service |
| `GET/POST /api/agents` · `POST /api/agents/:id/heartbeat` | Agent Registry |
| `GET /api/sync/bootstrap` · `GET /api/sync/audit` · `POST /api/sync/feedback` | Sync, audit ledger, feedback |

## Orchestrateur (MVP)

Simulation déterministe en attendant le LLM local (Phase 2): une tâche `queued` passe à `running` avec un plan, les demandes sensibles (write/patch/delete…) créent une approbation `waiting_permission`, et la décision de l'utilisateur reprend ou interrompt la tâche. Chaque événement est enregistré dans le ledger d'audit hash-chaîné (append-only).

## Phases suivantes (voir `swarmer/docs/23-implementation-plan.md`)

2. Orchestrateur LLM abliterated + tool-call JSON strict · 3. Swarm distribué (Redis Streams) · 4. Native bridge iPhone (contacts, calendrier, localisation…) · 5. Mémoire vectorielle (embeddings + FAISS) · 6. LLM on-device.
