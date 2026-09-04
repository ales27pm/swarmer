# 22 — Privacy and Data Governance

## Objectif

Garder la puissance locale sans accumuler un dépotoir de données privées impossible à nettoyer.

## Data categories

| Category | Examples | Storage | Default retention |
|---|---|---|---|
| Task data | prompts, task status | Ubuntu DB + iPhone cache | configurable |
| Memory | project facts, preferences | Ubuntu Memory Service | user-managed |
| iPhone data | location, contacts, photos metadata | transient unless approved | TTL short |
| Secrets | tokens, keys | SecureStore/secret vault | never to LLM |
| Audit | permission decisions/actions | append-only | long-term |
| Feedback | scores/corrections | Ubuntu DB | long-term after cleanup |
| Artifacts | patches/reports/files | disk | project policy |

## Rules

- Ne jamais mettre secrets dans prompts.
- Ne jamais stocker iPhone raw data sans raison.
- Toujours scoper contacts/photos/calendar.
- TTL obligatoire pour résultats sensibles.
- Memory write candidate pour faits personnels.
- User peut search/forget memory.
- Dataset export doit redacter PII.

## Redaction

Avant dataset:

- emails;
- phone numbers;
- addresses;
- tokens;
- API keys;
- location exact;
- private names si non nécessaires.

## Memory controls

App doit offrir:

- voir mémoire;
- corriger;
- supprimer;
- épingler;
- exporter;
- désactiver auto-memory.

## Consent

L'utilisateur doit approuver:

- accès iPhone sensible;
- règles persistantes;
- export dataset;
- fine-tuning sur données personnelles.

## Local-only mode

Aucun cloud requis. Les modèles, DB, vector index et artifacts résident localement. Les recherches web ou intégrations externes sont des capabilities explicitement déclarées.
