# monGARS Swarm App — Build Documents

Version: 0.1 draft  
Date: 2026-09-04  
Owner: ales27pm / 27PM  
Target: iPhone Expo app + Ubuntu local AI control plane + distributed autonomous swarm

## But

Ce paquet contient les documents nécessaires pour construire l'application **monGARS Swarm App**:

- application iPhone en Expo/React Native;
- modèle local on-device pour orchestration légère ou interface intelligente;
- control plane Ubuntu comme source de vérité;
- swarm d'agents autonomes distants;
- state partagé, message board, mémoire vectorielle et feedback loop;
- stratégie “abliterated où ça pense, strict où ça agit”.

## Principe central

Les modèles ne sont pas la couche de sécurité. Les modèles proposent des plans, appellent des outils ou demandent une permission. La **Permission Gateway** applique les règles, demande confirmation à l'utilisateur et contrôle les exécuteurs.

> Modèles = pensée et délégation.  
> Gateway = permission, risque, audit.  
> Executors = actions sandboxées.  
> Ubuntu = vérité officielle.  
> iPhone = interface, approbations, capteurs, cache local.

## Structure du paquet

```text
mongars-swarm-app-docs/
  README.md
  MASTER_SPEC.md
  docs/                Spécifications fonctionnelles et techniques
  adrs/                Architecture Decision Records
  api/                 Contrats API
  configs/             Configs de départ YAML
  schemas/             JSON Schemas de protocole
  prompts/             Prompts système par rôle
  diagrams/            Diagrammes Mermaid
  tests/               Plans de tests Expo, RN, backend et QA emulator
  checklists/          Checklists build, review et release
```

## Ordre de lecture recommandé

1. `MASTER_SPEC.md`
2. `docs/01-product-vision.md`
3. `docs/03-system-architecture.md`
4. `docs/04-mobile-expo-architecture.md`
5. `docs/05-ubuntu-control-plane.md`
6. `docs/08-state-memory-sync.md`
7. `docs/09-permission-gateway.md`
8. `docs/15-testing-strategy.md`
9. `docs/16-code-analysis-quality-gates.md`
10. `docs/23-implementation-plan.md`

## Mode de build visé

Phase 1 démarre avec Expo Go quand possible. Dès que le projet ajoute un module natif custom pour inférence locale, pont iPhone avancé ou intégration native non incluse dans Expo Go, il faut passer à un **development build** Expo.

## Stack cible

Mobile:

- Expo + React Native + TypeScript
- Expo Router
- SQLite local côté iPhone
- TanStack Query ou cache maison pour lecture/réconciliation
- WebSocket sécurisé avec Ubuntu
- SecureStore pour secrets courts
- modules Expo officiels pour caméra, location, contacts, calendrier, photos, audio
- development client dès qu'on ajoute MLX/llama.cpp/Core ML ou un bridge Swift custom

Ubuntu:

- FastAPI control plane
- llama.cpp/Ollama/vLLM-compatible OpenAI local endpoint
- SQLite WAL au MVP, Postgres ensuite
- Redis Streams au MVP, NATS JetStream ensuite si swarm distribué large
- FAISS au MVP, Qdrant ensuite si mémoire vectorielle multi-agent sérieuse
- append-only audit log
- pytest + ruff + mypy + bandit

## Non-objectifs du MVP

- Pas d'autonomie cachée.
- Pas d'envoi SMS/iMessage automatique sans UI utilisateur.
- Pas de contournement des permissions iOS.
- Pas d'auto-entraînement live sans revue, versioning et rollback.
- Pas de GitHub Actions obligatoire; les checks sont locaux par défaut.
