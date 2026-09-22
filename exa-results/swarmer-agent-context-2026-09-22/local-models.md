# Modèles locaux, embeddings et contraintes Apple

Recherche du 22 septembre 2026. Sept recherches Exa sur cet axe, puis une recherche
complémentaire sur les intégrations : 80 résultats retournés avant déduplication,
puis 24 pages récupérées séparément, dont quatre complètent l'axe orchestration.
Le journal contient les URL,
y compris les résultats écartés. Aucune bibliothèque installée, aucun modèle
téléchargé, aucune inférence ni modification du serveur de production.

## Décision recommandée

Conserver les moteurs déjà intégrés ; comparer des modèles sur nos tâches avant
de changer leurs valeurs par défaut. Ajouter les embeddings comme une capacité
mesurable, avec une version de modèle commune par index. Le meilleur score de
benchmark public n'établit ni la vitesse sur notre iPhone ni la fiabilité d'un
agent outillé.

## Compatibilité vérifiée dans le dépôt

L'iMac fournit Xcode 26.3, build 17C529. Le module natif fixe MLX Swift à 0.31.4,
MLX Swift LM à 3.31.4, swift-transformers à 1.3.0 et swift-huggingface à 0.9.0.
Il importe MLXLLM, MLXLMCommon et MLXHuggingFace, mais pas MLXEmbedders.
Le manifeste propose multilingual-e5-small pour les embeddings serveur et
bge-m3 comme option plus lourde : c'est une configuration, pas une mesure de
leur utilisation effective dans chaque requête.

Le [Package.swift du tag 3.31.4](https://github.com/ml-explore/mlx-swift-lm/blob/3.31.4/Package.swift)
expose MLXEmbedders, utilise Swift tools 6.1 et cible iOS 17 au minimum. Il
n'expose pas MLXGuidedGeneration. La [publication du tag](https://github.com/ml-explore/mlx-swift-lm/releases/tag/3.31.4)
prévient de changements de toolchain sur la branche principale.

Le [README actuel de MLX Swift LM](https://github.com/ml-explore/mlx-swift-lm)
présente MLXGuidedGeneration pour contraindre les sorties et un pont Foundation
Models nécessitant le SDK 27. Ce sont deux fonctionnalités distinctes : ne pas
attribuer automatiquement l'exigence SDK 27 au module de grammaires. Leur
intégration exacte avec notre toolchain reste à compiler et à qualifier. Le
[manifest de main](https://github.com/ml-explore/mlx-swift-lm/blob/main/Package.swift)
demande Swift tools 6.2 et permet de désactiver le trait FoundationModelsIntegration ;
le SDK 27 ne doit donc pas être déclaré obligatoire pour tout le package. La
licence du dépôt est MIT ; l'activité a été vérifiée par l'API GitHub, sans
utiliser le nombre d'étoiles comme preuve de fiabilité.

## Sélection d'embeddings à mesurer

| Candidat | Faits vérifiés | Place proposée | Limite à mesurer |
| --- | --- | --- | --- |
| [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) | Environ 118 M paramètres, 384 dimensions, entrées jusqu'à 512 tokens, licence MIT ; préfixes query/passage requis même en français | Référence légère du manifeste serveur, puis conversion locale qualifiée | Qualité FR/EN, fragmentation des documents et fidélité d'une conversion |
| [EmbeddingGemma 300M](https://ai.google.dev/gemma/docs/embeddinggemma/model_card) | 300 M, contexte 2K, 768 dimensions réductibles à 512/256/128, plus de 100 langues annoncées | Candidat mobile via MLXEmbedders ; Core ML en essai séparé | Licence Gemma et accès au modèle d'origine soumis à conditions ; coût mémoire/énergie à mesurer |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 0,6 B, contexte 32K, 32–1024 dimensions, plus de 100 langues ; Apache-2.0 | Comparaison qualité sur Ubuntu | Débit, latence et concurrence avec les modèles de génération |
| [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) | Modèle de classement distinct ; Apache-2.0 | Reclasser un petit ensemble de résultats sur Ubuntu | Le gain doit justifier le second passage et son coût |
| [Bekko a8m/a25m](https://huggingface.co/hotchpotch/bekko-embedding-v1-a25m) | MIT ; 384 dimensions, contexte 8K ; 8/25 M sont les paramètres actifs hors embeddings | Candidat exploratoire pour limiter le coût CPU | Résultats de l'auteur, compatibilité Core ML/MLX non démontrée ici |

Le [papier Bekko du 28 juillet 2026](https://arxiv.org/abs/2607.25180) rend cette
dernière piste intéressante, mais les débits CPU et classements annoncés sont
ceux de son protocole. Ajouter un essai Ubuntu reproductible avant d'envisager
une conversion mobile ; ne pas transformer le nombre de paramètres actifs en
estimation de la taille complète du modèle.

Les métadonnées Hugging Face, licences déclarées et révisions exactes consultées
sont dans `local-models-hf-metadata.json`. Elles figent la recherche, pas une
sélection définitive pour déploiement. Ne jamais mélanger directement les vecteurs
E5, Gemma et Qwen : une même dimension ne signifie pas un espace compatible. La
proposition est de séparer les index par modèle/révision, tokenizer, préfixe,
normalisation et dimension, puis de réindexer explicitement lors d'une migration.
Pour une mémoire partagée iPhone/Ubuntu, synchroniser les faits et leurs sources ;
la cohérence des textes ne dépend pas d'un partage obligatoire des vecteurs.

## Core ML et GPU : candidats, pas promesses de performance

[CoreML-LLM](https://github.com/john-rocky/CoreML-LLM), sous MIT, propose des
adaptateurs Core ML et des bundles téléchargeables, notamment EmbeddingGemma.
Le mainteneur publie des mesures sur iPhone 17 Pro et explique son protocole.
Ce sont des mesures de l'auteur sur un autre matériel ; elles ne prouvent ni
la résidence ANE ni le débit soutenu sur notre iPhone 16 Pro. Les licences des
poids restent distinctes de celle de la bibliothèque. À tester dans un adaptateur
isolé, avec comparaison numérique des embeddings, mémoire et énergie.

L'article [Core ML d'Apple sur Llama](https://machinelearning.apple.com/research/core-ml-on-device-llama)
illustre aussi une optimisation visant le GPU d'un Mac. Le format Core ML ne
garantit donc pas à lui seul une exécution sur le Neural Engine. La preuve doit
venir de l'appareil, avec son modèle compilé et ses options de calcul.

## Sorties structurées et mémoire de calcul

[Ollama](https://docs.ollama.com/capabilities/structured-outputs) et
[llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)
documentent des sorties guidées par schéma/grammaire. Cela aide la syntaxe,
mais ne garantit ni les arguments, ni la pertinence d'une action, ni l'achèvement
d'une réponse interrompue. Conserver les validations métier, les limites de
durée et les reçus d'exécution déjà présents.

Deux discussions de première main donnent des cas de régression à reproduire :

- [Ollama #16563](https://github.com/ollama/ollama/issues/16563) rapporte que la
  voie MLX ignore un schéma dans la configuration décrite. Le ticket ne prouve
  rien sur notre voie Ollama Ubuntu ni sur MLX Swift directement.
- [MLX Swift LM #312](https://github.com/ml-explore/mlx-swift-lm/issues/312)
  décrit une perte de contexte après remplacement du cache KV quantifié, avec
  une discussion du mainteneur sur sa propriété. C'est un scénario de test,
  pas une affirmation que notre app subit ce défaut.

La compaction sémantique de l'historique, la quantification du cache KV et la
réutilisation d'un préfixe sont trois mécanismes différents. Ils nécessitent
respectivement des tests de fidélité, de cohérence entre tours et d'invalidation.

## Génération 3–5B : conserver une comparaison réaliste

Comparer le Dolphin 3B actuel à [Qwen3-4B MLX 4-bit](https://huggingface.co/Qwen/Qwen3-4B-MLX-4bit)
et à un artefact qualifié de [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B).
Les cartes officielles décrivent leurs formats et capacités ; aucune mesure
comparative sur notre iPhone n'a été réalisée pendant cette recherche. Qwen3
documente un réglage explicite du mode de raisonnement : contrôler ce budget
dans le template, et ne pas remplacer ses recommandations par une température
universelle supposée fiable.

Attention au nom [Gemma 4 E4B](https://ai.google.dev/gemma/docs/core/model_card_4) :
la carte distingue 4,5 B paramètres effectifs et environ 8 B avec embeddings.
Ce n'est pas l'équivalent mémoire d'un modèle dense de 4 B. Même précaution
avec les paramètres actifs d'un modèle MoE. Candidat secondaire si le budget
mémoire réel le permet, pas remplacement immédiat du preset 3B.

Les variantes dites abliterated/uncensored ne constituent pas, par leur nom,
une garantie de programmation, d'utilisation d'outils ou de JSON correct.
Notre classement proposé doit porter sur ces résultats observables.

## Évaluation proposée

Utiliser [MTEB](https://github.com/embeddings-benchmark/mteb) pour un sous-ensemble
français/anglais reproductible, puis un corpus propre à monGARS : décisions
contradictoires, exigences anciennes, clients homonymes, références de fichiers
et chronologie. L'[issue française #1314](https://github.com/embeddings-benchmark/mteb/issues/1314)
montre pourquoi versions des jeux de données et auto-déclarations des scores
doivent être vérifiées avant de comparer des moyennes.

Mesurer rappel@k, nDCG, exactitude des sources, contamination entre projets,
latence p50/p95, mémoire maximale et coût d'indexation. Pour l'iPhone, ajouter
énergie et stabilité pendant un essai soutenu. Pour la génération : choix de
l'outil, arguments valides, tâche effectivement terminée, messages fidèles aux
reçus et arrêt correct après interruption. Aucun de ces scores locaux n'est
encore produit par cette recherche.
