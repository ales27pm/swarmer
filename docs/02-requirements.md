# 02 — Requirements

## Functional Requirements

### FR-001 — Chat / command interface

L'app iPhone doit permettre d'envoyer une commande texte ou voix vers l'orchestrateur.

### FR-002 — Task lifecycle

Chaque demande devient une tâche avec états:

- `created`
- `planned`
- `waiting_permission`
- `queued`
- `running`
- `blocked`
- `completed`
- `failed`
- `cancelled`

### FR-003 — Agent routing

L'orchestrateur doit choisir un ou plusieurs agents selon l'intention, les skills disponibles et le contexte.

### FR-004 — Agent registry

Le control plane doit maintenir un registry des agents:

- agent id;
- endpoint;
- skills;
- modèle;
- version;
- health status;
- permissions max;
- tags.

### FR-005 — Shared state

Toutes les composantes doivent lire/écrire l'état via le State Service, jamais directement.

### FR-006 — Message board

Les agents doivent communiquer via un message board durable avec enveloppes typées.

### FR-007 — Memory Service

Le système doit stocker et retrouver du contexte long terme via embeddings.

### FR-008 — iPhone Native Bridge

L'iPhone doit exposer des capabilities natives selon permissions:

- contacts;
- calendrier;
- rappels;
- localisation;
- photos;
- caméra;
- micro;
- notifications;
- appels/messages/email en mode compose/prepare quand iOS l'impose.

### FR-009 — Permission Gateway

Toute action sensible doit passer par une demande structurée et une décision explicite.

### FR-010 — Feedback loop

Chaque tâche doit produire des événements de feedback exploitables.

### FR-011 — Local-first sync

L'iPhone doit conserver une replica locale et synchroniser avec Ubuntu.

### FR-012 — Offline queue

L'iPhone doit accepter certaines actions hors ligne et les pousser au retour réseau.

### FR-013 — Audit log

Chaque décision/action doit être append-only loggée avec hash chaîné optionnel.

### FR-014 — Model manifest

Les modèles doivent être déclarés dans un manifest avec rôle, quant, endpoint, contexte et policy.

## Non-Functional Requirements

### NFR-001 — Local-first

Le système doit fonctionner sur réseau local sans cloud obligatoire.

### NFR-002 — Minimal VRAM

Sur carte 8 Go, garder un ou deux petits modèles actifs; charger les autres à la demande.

### NFR-003 — Typed contracts

Les tool calls, messages, permissions et sync ops doivent valider contre JSON Schema.

### NFR-004 — Observability

Logs lisibles, task timeline, agent heartbeat, health endpoint.

### NFR-005 — Security

Pairing device, auth token, TLS local si possible, scopes, sandbox, allowlists.

### NFR-006 — Testability

Toutes les décisions critiques doivent être testables sans LLM live via fixtures.

### NFR-007 — No surprise autonomy

Aucune action destructive, externe ou sensible sans approbation ou règle explicite préapprouvée.

## Constraints

- Expo/React Native pour l'app.
- Ubuntu comme serveur local.
- Modèles locaux, idéalement Q4.
- Préférence pour modèles abliterated pour les agents raisonneurs.
- Couche permissions déterministe.
- Pas de GitHub Actions obligatoire; checks locaux.
- iOS ne permet pas un accès brut illimité à toutes les fonctions système.
