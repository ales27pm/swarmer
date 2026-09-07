# monGARS Swarm App — Build Documents

Version: 0.6 local MVP
Date: 2026-09-04  
Owner: ales27pm / 27PM  
Target: iPhone Expo app + Ubuntu local AI control plane + distributed autonomous swarm

## But

Ce paquet contient les documents nécessaires pour construire l'application **monGARS Swarm App**:

- application iPhone en Expo/React Native;
- modèle local on-device pour orchestration légère ou interface intelligente;
- control plane Ubuntu comme source de vérité;
- swarm d'agents autonomes distants;
- state partagé, conversations, mémoire structurée locale et feedback loop;
- stratégie “abliterated où ça pense, strict où ça agit”.

## Principe central

Les modèles ne sont pas la couche de sécurité. Les modèles proposent des plans, appellent des outils ou demandent une permission. La **Permission Gateway** applique les règles, demande confirmation à l'utilisateur et contrôle les exécuteurs.

> Modèles = pensée et délégation.  
> Gateway = permission, risque, audit.  
> Executors = actions sandboxées.  
> Ubuntu = vérité officielle.  
> iPhone = interface, approbations, capteurs, cache local.

## État implémenté

Le control plane local conserve ses fondations réelles d'authentification, de
modèle et d'exécution:

- émission d'un code d'appairage uniquement depuis une connexion loopback ayant
  aussi le secret opérateur; code unique, expirant, à tentatives limitées;
- jeton opaque par appareil pour toutes les API de ressources, conservé sous
  forme de digest côté serveur; un nouvel appairage crée d'abord un candidat
  court, limité au bootstrap et à sa finalisation. L'ancien jeton reste valide
  jusqu'à ce que le candidat soit durable dans SecureStore puis utilisé pour un
  bootstrap de bascule. Une trace SecureStore séparée permet de reprendre une
  réponse perdue ou de revenir à la connexion active si le candidat expire. Les
  anciennes clés URL/jeton séparées ne sont jamais recombinées;
- ticket WebSocket de 30 secondes, consommable une seule fois, afin de ne jamais
  mettre le jeton d'appareil longue durée dans une URL;
- tâches enrichies, conversations/messages, appels d'outils, approbations,
  mémoire, agents, audit chaîné par hash et feedback;
- recherche mémoire actuelle explicitement **lexicale**; la recherche vectorielle
  demeure une évolution, pas une capacité annoncée;
- planification par le modèle local sans minuterie ni succès simulé: seul un
  résultat réel de l'exécuteur peut terminer une tâche;
- création appel-outil/approbation et décisions atomiques, non rejouables. Une
  tâche annulée ou terminale ne peut pas être ressuscitée. « Autoriser une fois »
  n'installe aucune règle persistante. Chaque accord est lié à l'identifiant,
  au nom et aux arguments canoniques exacts de l'appel; l'iPhone affiche une
  cible/commande expurgée et l'empreinte. Le texte libre du modèle est remplacé
  par un libellé serveur fixe pour tout appel d'outil et ne devient jamais une
  base de consentement;
- `process.run` isolé avec Bubblewrap sans réseau, capacités ni environnement
  hôte, soumis à des limites POSIX de CPU, mémoire, processus, fichiers et
  descripteurs, limité aux arguments/commandes autorisés, et associé à des accès
  fichiers par descripteurs résistants aux échanges de symlinks. Après
  redémarrage, une exécution déjà revendiquée devient `failed` avec résultat
  incertain. Un appel approuvé mais encore antérieur à la revendication devient
  `failed` comme non démarré. Aucun des deux n'est rejoué automatiquement.
  Les alias par lien physique d'un chemin protégé sont aussi refusés. Si
  `/usr/bin/bwrap` manque, l'exécution est refusée sans fallback direct.

Le contrat complet est dans [`api/openapi.yaml`](api/openapi.yaml) et la procédure
locale dans [`docs/18-dev-setup.md`](docs/18-dev-setup.md).

## Structure du dépôt

```text
mongars-swarm/
  README.md
  MASTER_SPEC.md
  docs/                Spécifications fonctionnelles et techniques
  adrs/                Architecture Decision Records
  api/                 Contrat OpenAPI de l'API locale authentifiée
  configs/             Configs de départ YAML
  schemas/             JSON Schemas de protocole
  prompts/             Prompts système par rôle
  diagrams/            Diagrammes Mermaid
  server/tests/        Tests exécutables du control plane
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

Ubuntu — présent dans le MVP:

- FastAPI control plane
- llama.cpp/Ollama/vLLM-compatible OpenAI local endpoint
- SQLite WAL au MVP, Postgres ensuite
- append-only audit log
- pytest + ruff + mypy + bandit

Ubuntu — évolutions ciblées, non annoncées comme déjà livrées:

- Redis Streams puis NATS JetStream si le swarm distribué le justifie
- mémoire vectorielle FAISS ou Qdrant; le MVP actuel utilise une recherche lexicale

## Non-objectifs du MVP

- Pas d'autonomie cachée.
- Pas d'envoi SMS/iMessage automatique sans UI utilisateur.
- Pas de contournement des permissions iOS.
- Pas d'auto-entraînement live sans revue, versioning et rollback.
- Pas de GitHub Actions obligatoire; les checks sont locaux par défaut.
