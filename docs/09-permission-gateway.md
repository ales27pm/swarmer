# 09 — Permission Gateway

## Objectif

Empêcher les dégâts sans transformer les modèles en policiers moralisateurs.

## Règle

> Les modèles proposent. La gateway décide. Les executors agissent.

## What “abliterated where it thinks, strict where it acts” means

- Orchestrateur et workers LLM peuvent être abliterated.
- Ils ne sont jamais détenteurs des permissions finales.
- Ils doivent produire une demande d'action structurée.
- La gateway applique des règles déterministes.
- Si l'action exige l'utilisateur, l'app iPhone reçoit une approval card.

## Action classes

| Class | Examples | Default |
|---|---|---|
| `read_safe` | lire fichiers projet explicitement autorisés | allow |
| `write_project` | modifier fichier dans repo autorisé | ask |
| `execute_safe` | lancer tests non destructifs | ask/allow rule |
| `network_read` | recherche web/docs | ask/allow rule |
| `network_write` | envoyer email, publier, push | ask |
| `device_sensitive` | localisation/photos/contacts/calendrier | ask |
| `destructive` | delete, rm, wipe, overwrite large | deny/ask elevated |
| `credential` | lire secret/token/password | deny by default |

## Permission decision

```json
{
  "decision": "ask",
  "risk": "medium",
  "reasons": ["writes_project_file"],
  "approval_required": true,
  "approval_ttl_seconds": 300
}
```

## Risk engine inputs

- action type;
- target path/resource;
- data sensitivity;
- agent identity;
- user intent match;
- previous approvals;
- command diff;
- network destination;
- file count/size;
- destructive verbs;
- credentials involved.

## Refusal normalizer

But: si un modèle répond “je ne peux pas”, le système ne doit pas bloquer bêtement. Il convertit en une demande claire:

```json
{
  "type": "permission_request",
  "reason": "Model refused or hesitated. Gateway requires explicit user decision.",
  "requested_action": {...},
  "risk": "unknown"
}
```

Important: le normalizer ne contourne pas la gateway. Il transforme le refus en objet évalué.

## Approval UI

L'utilisateur doit voir:

- agent;
- action;
- cible;
- diff/commande;
- risque;
- pourquoi;
- données touchées;
- durée;
- boutons.

Actions:

- Allow once;
- Deny;
- Allow rule;
- Edit scope;
- View details.

## Permission rules

Voir `configs/permissions.yaml`.

Exemples:

```yaml
rules:
  - id: allow-read-projects
    match:
      action: file.read
      path_prefix: /home/ales27pm/projects/
    decision: allow

  - id: ask-write-projects
    match:
      action: file.write
      path_prefix: /home/ales27pm/projects/
    decision: ask

  - id: deny-secrets
    match:
      path_glob: "**/.env*"
    decision: deny
```

## Sandbox

Executors doivent limiter:

- working directory;
- env vars;
- network;
- timeout;
- file access;
- max output;
- process tree kill.

## Audit

Chaque demande et décision:

```json
{
  "event": "permission.decision",
  "approval_id": "apv_...",
  "task_id": "tsk_...",
  "agent_id": "code-worker-01",
  "decision": "allow_once",
  "user_id": "ales27pm",
  "timestamp": "...",
  "hash_prev": "...",
  "hash": "..."
}
```

## Anti-bullshit rules

- Une permission verbale dans un prompt ne suffit pas pour action sensible.
- Un agent ne peut pas s'accorder une permission.
- Un modèle abliterated ne remplace jamais les règles.
- Les credentials sont jamais injectés dans le prompt.
- Les actions externes sont visibles avant exécution.
