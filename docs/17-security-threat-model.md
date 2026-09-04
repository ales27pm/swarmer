# 17 — Security Threat Model

## Objectif

Créer un système puissant sans buffet ouvert pour agents, prompts ou réseau.

## Assets à protéger

- fichiers projets;
- secrets/tokens;
- photos/contact/location/calendar;
- mémoire long terme;
- audit logs;
- capacité d'exécuter shell;
- comptes email/GitHub/Cloud;
- réseau local.

## Trust boundaries

```text
User ↔ iPhone App ↔ Network ↔ Ubuntu API ↔ LLM ↔ Gateway ↔ Executors ↔ Files/Internet
                       ↘ Message Board ↔ Remote Workers
```

## Menaces

### Prompt injection

Un contenu externe tente de convaincre l'agent d'ignorer les règles.

Mitigation:

- outils typés;
- gateway;
- prompt isolation;
- source labels;
- no secrets in prompt;
- retrieval filtering.

### Tool injection

Un agent invente un tool ou arguments dangereux.

Mitigation:

- allowlist;
- schema validation;
- unknown tool blocked.

### Permission escalation

Un agent demande une action plus large que nécessaire.

Mitigation:

- scopes;
- risk engine;
- human approval;
- diff view.

### Data exfiltration

Un agent tente de lire/envoyer secrets ou données privées.

Mitigation:

- deny `.env*`, keys, password stores;
- network write ask;
- redaction;
- no direct outbound for executors by default.

### Remote worker compromise

Un worker distant devient hostile.

Mitigation:

- per-agent tokens;
- scoped skills;
- registry health;
- sandbox;
- no DB direct access;
- revoke agent.

### iPhone sensitive data overexposure

Un agent demande trop de contacts/photos/location.

Mitigation:

- capability broker;
- minimal fields;
- TTL;
- user approval;
- local redaction.

### Model hallucination

Le modèle invente state/fichiers/permissions.

Mitigation:

- state lookup;
- schema validation;
- citations/artifact ids;
- confirm before write.

## Default-deny zones

- credentials;
- destructive filesystem actions;
- external network write;
- money/payment;
- hidden background tracking;
- bulk personal data export;
- arbitrary shell outside sandbox.

## Logging policy

Logs doivent inclure:

- trace id;
- actor;
- action;
- decision;
- target hash/path;
- result status.

Logs ne doivent pas inclure:

- raw tokens;
- passwords;
- full private data unless explicitly enabled.

## Incident actions

- pause orchestrator;
- revoke device token;
- revoke agent token;
- freeze permissions;
- export audit;
- restore DB backup;
- rebuild vector index.
