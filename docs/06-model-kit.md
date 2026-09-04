# 06 — Model Kit

## Objectif

Choisir des modèles qui tournent localement, ne mangent pas toute la VRAM et se spécialisent par rôle.

## Stratégie

- 3B/4B Q4 pour la plupart des agents.
- Un orchestrateur actif.
- Un worker actif à la fois au MVP si VRAM limitée.
- Embedding model CPU/GPU léger séparé.
- Modèles déclarés dans `configs/model-manifest.yaml`.
- Tous les appels passent par un `Model Router` local, même si le routeur n'est pas un LLM autonome.

## Principe abliterated

Le système peut utiliser des modèles abliterated pour l'orchestrateur et les workers raisonneurs afin de réduire les refus inutiles. Mais ces modèles n'ont pas le droit d'exécuter directement. Ils sortent:

- un plan;
- un tool call;
- une demande de permission;
- un résumé.

La gateway décide.

## Recommandation MVP

### Orchestrateur principal

`mradermacher/Hermes-3-Llama-3.2-3B-abliterated-GGUF:Q4_K_M`

Rôle:

- planification;
- routing;
- tool-call JSON;
- conversation;
- demande de permission.

### Worker rapide abliterated

`mradermacher/G9v3-3B-Heretic-Abliterated-GGUF:Q4_K_M`

Rôle:

- tâches courtes;
- analyse rapide;
- reformulation;
- small code reasoning;
- fallback autonome.

### Worker Dolphin fallback

`bartowski/Dolphin3.0-Llama3.2-3B-GGUF:Q4_K_M`

Rôle:

- style conversationnel;
- fallback si Hermes/G9 répond mal;
- tests de comportement.

### Orchestrateur benchmark non-abliterated

`katanemo/Plano-Orchestrator-4B`

Rôle:

- référence de qualité pour routing/orchestration;
- pas nécessairement dans le mode full abliterated;
- utile pour générer des exemples d'entraînement ou comparer.

## Embeddings

### MVP léger

`intfloat/multilingual-e5-small`

Usage:

- mémoire courte/moyenne;
- rapide;
- FR/EN correct;
- CPU acceptable.

### Qualité plus robuste

`BAAI/bge-m3`

Usage:

- mémoire long terme;
- meilleur multilingual;
- documents plus longs;
- hybrid retrieval possible.

## Profiles

### Profile `iphone-edge`

- pas de modèle lourd requis au départ;
- modèle local plus tard via MLX/Core ML;
- cache, summarizer et classifier possible.

### Profile `ubuntu-vram-8gb`

- orchestrateur 3B Q4 actif;
- embedding model CPU;
- 1 worker 3B Q4 chargé à la demande;
- éviter plusieurs 7B simultanés.

### Profile `ubuntu-plus-workers`

- orchestrateur Ubuntu;
- workers sur machines séparées;
- message board central;
- chaque worker héberge son modèle spécialisé.

## Model response contract

Tout modèle doit répondre avec un des types:

- `final_answer`
- `plan`
- `tool_call`
- `permission_request`
- `ask_clarification`
- `memory_write_candidate`
- `error_report`

Pas de prose libre pour les actions.

## Anti-patterns

- Laisser un modèle écrire directement dans fichiers/DB.
- Mettre les secrets dans le prompt.
- Laisser un agent inventer ses permissions.
- Charger trop de modèles en VRAM.
- Confondre abliterated avec “pas de sécurité”.
