# 06 — Model Kit

> **Statut:** le control plane appelle des endpoints compatibles OpenAI et
> conserve les modèles par rôle. L’app iOS propose des préréglages Dolphin
> épinglés pour MLX, GGUF et Core ML. Les tests logiciels ne constituent pas
> une mesure de qualité ou de performance sur un iPhone.

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

- runtime local facultatif via Core ML, MLX ou llama.cpp/GGUF;
- un seul modèle chargé et une seule génération active;
- génération bornée à 512 nouveaux jetons par l'interface native;
- les sorties restent des propositions locales non vérifiées;
- aucune authentification et aucune exécution d’outil dans le module natif.

### Dolphin 3B pour les itérations iOS

Sélection vérifiée sur Hugging Face le **12 septembre 2026**:
[Dolphin3.0-Llama3.2-3B](https://huggingface.co/dphn/Dolphin3.0-Llama3.2-3B),
3,21 milliards de paramètres, licence Llama 3.2. Cette famille dispose
d’artefacts dans les trois formats. Dolphin est un fine-tune non censuré;
sa fiche ne déclare pas d’ablitération. Hermes et G9 restent les préréglages
abliterated du swarm.

| Runtime | Préréglage | Poids | Contexte de l’app | Installation |
| --- | --- | --- | --- | --- |
| MLX | [mlx-community/dolphin3.0-llama3.2-3B-4Bit](https://huggingface.co/mlx-community/dolphin3.0-llama3.2-3B-4Bit) | 1,807 Go | 4 096 jetons | Dépôt et commit préremplis; téléchargement au premier chargement explicite |
| GGUF | [bartowski/Dolphin3.0-Llama3.2-3B-GGUF](https://huggingface.co/bartowski/Dolphin3.0-Llama3.2-3B-GGUF), Q4_K_M | 2,019 Go | 4 096 jetons | Bouton de téléchargement dans l’app avec contrôle de taille et SHA-256, ou import de fichier |
| Core ML | [ales27pm/Dolphin3.0-CoreML](https://huggingface.co/ales27pm/Dolphin3.0-CoreML), stateful INT4 | 1,809 Go | 2 048 jetons | Import du dossier contenant le package et ses fichiers tokenizer/configuration |

Le modèle source date du 30 décembre 2024. Les dates de conversion plus
récentes ne désignent pas une nouvelle génération de Dolphin. Le
[Dolphin3.0-Qwen2.5-3b](https://huggingface.co/dphn/Dolphin3.0-Qwen2.5-3b)
du 3 janvier 2025 est légèrement plus récent, mais utilise la licence
qwen-research et aucun équivalent Core ML compatible n’a été trouvé.
Le Dolphin X1 Trinity Nano observé dans la famille officielle compte 6B
paramètres, au-delà de la plage demandée. Le choix Llama 3B privilégie la
compatibilité des formats; aucune supériorité de benchmark n’est revendiquée.

Dans **Réglages → Ouvrir les modèles locaux**, MLX est sélectionné quand il
est disponible. Le chargement et le téléchargement restent explicites.
Les réglages enregistrent le runtime, le modèle, sa révision, la température
et la limite de sortie sur l’iPhone. Par défaut: **256 jetons, température
0,1**; la limite native reste 512. Les choix personnalisés sont conservés
lors d’un changement de runtime. Une génération ou un téléchargement annulé
ne déclenche aucun chargement ni soumission.

Les SHAs complets et identités des fichiers figurent dans
[`configs/model-manifest.yaml`](../configs/model-manifest.yaml) et dans le
catalogue mobile `mobile/src/lib/local-model-presets.ts`. Les références sont
figées: aucune résolution automatique de `main` au lancement. MLX et GGUF
utilisent le template de conversation du modèle; le preset Core ML Dolphin
emploie également celui de son tokenizer.

Pour préparer le dossier Core ML sur le Mac avec la CLI Hugging Face:

```sh
hf download ales27pm/Dolphin3.0-CoreML \
  --revision c786a7060b183baa9ce8f8b11dded70f6d88e021 \
  --include 'Dolphin3.0-Llama3.2-3B-stateful-int4.mlpackage/**' \
    config.json generation_config.json tokenizer.json tokenizer_config.json \
    special_tokens_map.json \
  --local-dir ./Dolphin3B-CoreML
```

Transférer ce dossier dans Fichiers, puis utiliser **Importer un dossier Core
ML**. Ne sélectionner que le package recommandé, avec les sidecars à côté;
un dossier contenant plusieurs exports du modèle est ambigu. L’adaptateur
valide `inputIds`, le masque causal FP16, les deux états KV et la forme des
logits; il traite les prompts par segments de 512 jetons et crée un nouvel
état à chaque génération. iOS 18 minimum.

La RAM nécessaire dépasse la taille des poids: le package Core ML ajoute
environ 235 Mo de cache KV, plus les activations et le tokenizer. Le
téléchargement GGUF exige de l’espace pour le fichier temporaire et sa copie
privée, plus 1 Gio de réserve, et s’annule en arrière-plan. Les débits,
pics mémoire et sorties JSON avec ces poids restent à mesurer sur un iPhone.

Les commandes de lancement et les règles d’héritage des rôles du serveur
sont décrites dans [`server/README.md`](../server/README.md).

Les dépendances natives sont épinglées: `swift-transformers` 1.3.0,
`mlx-swift` 0.31.4, `mlx-swift-lm` 3.31.4,
`swift-huggingface` 0.9.0 et le XCFramework llama.cpp `b10809` vérifié par
SHA-256 puis embarqué et signé par CocoaPods. Un modèle MLX distant exige un SHA de commit complet; `main`, une
branche ou une étiquette mobile ne sont jamais présentés comme une révision
reproductible.

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
