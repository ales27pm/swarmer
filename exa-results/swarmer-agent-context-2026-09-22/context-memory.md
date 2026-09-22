# Mémoire et ingénierie du contexte pour Swarmer

Recherche arrêtée au **22 septembre 2026**. Fenêtre récente : **22 mars–22 septembre 2026** ; des travaux antérieurs servent de fondations. Recherche documentaire seulement : aucun modèle, déploiement, changement de routage ou accès aux données de production. Les recommandations ci-dessous sont à qualifier ; elles ne décrivent pas des fonctions déjà livrées.

## Décision proposée

**Conserver le runtime FastAPI/SQLite et améliorer d’abord sa sélection du contexte et ses données de reprise.** Les sources ne justifient ni une migration complète vers un framework d’agents, ni l’idée qu’un graphe ou davantage de contexte améliore systématiquement les résultats. Le meilleur premier investissement est un état de reprise structuré, versionné et relié aux événements d’origine, accompagné de récupération ciblée. La compaction générative devient un auxiliaire réversible, pas la source de vérité.

La priorité locale est cohérente avec le worker projet inspecté : limites de 22 000 octets de prompt et 2 000 jetons de sortie, sélection des fichiers, historique réduit sous pression, fenêtre Ollama configurée à 32 768. Ce sont des bornes de transport et des heuristiques de sélection ; elles ne prouvent pas que les exigences anciennes demeurent accessibles ni qu’un résumé durable est produit. Voir `workers/project-worker/project_worker.py:48`, `:865`, `:1137`, `:1230`.

## Méthode et couverture

- Six recherches Exa, dix résultats demandés par recherche : **60 emplacements de récupération et 60 résultats retournés**. Leurs URL exactes sont différentes, mais plusieurs correspondent au même travail sous un autre format, version ou paramètre de suivi.
- Dix extraits primaires principaux, puis cinq lectures complémentaires pour vérifier dates, concepts et portée : **15 URL récupérées, 14 sources logiques**. Des extraits sont tronqués ; « récupéré » ne signifie pas « article intégralement lu ».
- Treize lectures de l’API publique GitHub : cinq métadonnées de dépôts, quatre issues/PR et leurs quatre listes de commentaires. Les associations d’auteurs et les dates ont été conservées.
- Les requêtes exactes, URL retournées, dates de découverte et tailles des extraits sont dans `context-memory-sources.json`. Les métadonnées GitHub sont dans `context-memory-github.json`. Les pages secondaires trouvées servent uniquement à la découverte, pas aux conclusions techniques.

## Ce que les sources établissent

### 1. Anthropic : séparer le journal durable de la fenêtre du modèle

L’article du 29 septembre 2025 recommande une sélection compacte des informations utiles, la récupération au moment opportun par identifiants légers et des notes structurées. Il décrit la compaction comme une opération avec perte et invite à préserver les décisions et problèmes non résolus. Ce retour d’ingénierie vient du fournisseur ; il ne mesure pas les modèles locaux de Swarmer. [Article context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

Le retour du **8 avril 2026**, dans la fenêtre récente, rend cette séparation plus concrète : journal de session durable en ajout seulement, harness et environnement d’exécution séparés, récupération d’anciens événements par position. Le journal permet de retrouver une information retirée de la fenêtre. À adapter dans Swarmer : références stables aux événements, résultats et artefacts ; un résumé ne remplace jamais leur stockage. Les gains de latence de leur infrastructure gérée ne sont pas transférables à notre hôte Ubuntu. [Managed agents](https://www.anthropic.com/engineering/managed-agents).

### 2. Letta/MemGPT : blocs explicites utiles, mémoire autoéditée à encadrer

Les blocs Letta ont un nom, une description, une valeur et une limite de caractères. Ils peuvent être présents en permanence dans le prompt, partagés entre agents et marqués en lecture seule. Cette forme de mémoire explicite est pertinente pour les contraintes utilisateur, le profil du projet et un état de reprise court. La documentation confirme le mécanisme, pas sa fiabilité pour notre modèle. [Memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks/index.md).

**Adapter l’idée des blocs, différer le remplacement du runtime.** Un bloc partagé modifiable n’est pas automatiquement une vérité commune : il faut une portée projet/utilisateur, une version, la provenance des faits, une politique de résolution des conflits et des écritures conditionnelles. Les contraintes de sécurité et l’autorisation d’exécuter restent déterministes, même si un agent peut modifier son carnet de travail. Le nom MemGPT désigne ici la famille historique de conception de Letta ; aucun score du papier MemGPT n’est utilisé sans vérification directe.

### 3. LangGraph/LangMem : une taxonomie et des composants, pas une obligation de migration

LangGraph distingue les checkpoints d’un fil de conversation des mémoires partagées entre fils, placées dans des espaces de noms. LangMem distingue faits sémantiques, expériences et procédures ; profils uniques et collections ont des compromis différents. La collection augmente la souplesse du rappel mais rend les mises à jour, suppressions et contradictions plus difficiles. La documentation reconnaît aussi les modèles qui ajoutent trop ou réécrivent trop. [LangGraph memory](https://docs.langchain.com/oss/python/langgraph/memory), [LangMem concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/).

**Adapter la séparation checkpoint/mémoire et les schémas d’extraction.** Conserver les leases, budgets, reçus et transactions Swarmer. Une extraction asynchrone peut raccourcir le chemin interactif, mais son coût, son retard et ses conflits restent à mesurer. Ne pas donner à un optimiseur de prompt le droit de réécrire silencieusement les politiques de l’application. Une bibliothèque limitée peut être évaluée derrière une interface, sans confier tout l’ordonnancement à LangGraph.

### 4. Mem0 : piste d’extraction, résultats commerciaux à attribuer précisément

Le papier Mem0 décrit une extraction/consolidation des faits et une variante à relations de graphe. Il annonce un gain relatif d’environ 26 % sur un score jugé par modèle et de fortes baisses de latence et de jetons face au contexte complet, sur LoCoMo. Il s’agit d’un papier rédigé par l’équipe Mem0 ; ce chiffre n’est ni 26 points d’exactitude, ni une preuve indépendante de supériorité sur nos tâches. [Papier Mem0](https://arxiv.org/html/2504.19413v1).

**Évaluer éventuellement l’extracteur, différer une adoption par défaut.** Les faits extraits doivent citer un événement original, distinguer utilisateur/agent/outil, et pouvoir être rejetés, corrigés ou supprimés. L’ingestion ne doit pas réextraire les souvenirs injectés au tour précédent. Les résultats du papier et les fonctions annoncées par le service hébergé ne prouvent pas la présence du même comportement dans une version OSS locale donnée.

### 5. Zep/Graphiti : temporalité intéressante, coût d’intégration réel

Graphiti documente des épisodes, entités et relations temporelles, avec recherche combinant texte, similarité et graphe. La conservation des relations invalidées est pertinente pour répondre « à telle date » sans mélanger état ancien et état actuel. Le dépôt présente des backends de graphe séparés, notamment Neo4j, FalkorDB et Neptune/OpenSearch. L’OSS Graphiti et le service Zep ne doivent pas être assimilés. [Dépôt et documentation Graphiti](https://github.com/getzep/graphiti).

**Adapter d’abord la temporalité dans SQLite** : faits atomiques, provenance, dates d’observation/validité et liens de remplacement ou contradiction. Différer le graphe complet jusqu’à ce qu’un benchmark montre un gain sur des questions multi-entités et temporelles qui compense l’exploitation d’un datastore supplémentaire et les appels d’extraction/résolution.

## Benchmarks : ce qu’ils permettent de tester, et leurs limites

| Source primaire | Résultat utile | Limite pour Swarmer |
|---|---|---|
| [LongMemEval, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf) | 500 questions ; extraction, raisonnement entre sessions, temporalité, mise à jour des connaissances et abstention. Sépare indexation, récupération et lecture. | QA personnelle, pas une preuve de modification de fichiers, de tests exécutés ou de respect des permissions. |
| [LoCoMo, ACL 2024](https://aclanthology.org/2024.acl-long.747/) | Conversations longues produites par une chaîne hybride puis vérifiées/éditées humainement ; difficulté de causalité et temporalité. | Distribution construite, multimodale et personnelle ; nécessite adaptation à nos projets et messages en français. |
| [Lost in the Middle, TACL 2024](https://aclanthology.org/2024.tacl-1.9/) | La position d’une information dans un long contexte peut dégrader fortement son utilisation. | Modèles et tâches plus anciens ; résultat qualitatif à retester, pas estimation du taux d’erreur actuel. |
| [The Complexity Trap](https://arxiv.org/html/2508.21433) | Sur les configurations étudiées d’agents de code, masquer les anciennes observations peut égaler la synthèse par LLM à moindre coût ; la synthèse n’est pas systématiquement gagnante. | Les outils, modèles et benchmarks diffèrent ; il faut garantir la récupération des observations masquées. |

Ce dernier travail motive un essai simple : remplacer les anciens gros retours d’outils par des identifiants et extraits minimaux, tout en conservant l’original récupérable. Il ne justifie pas de retirer aveuglément les contraintes utilisateur, les erreurs encore ouvertes ou les preuves de test.

## Travaux récents : candidats de recherche, pas dépendances à déployer

**Harness the Memory**, soumis le **15 août 2026**, annonce une comparaison contrôlée de substrats sur trois modèles, quatre suites et 26 mesures. Son résumé rapporte qu’aucun substrat ne domine partout : une récupération large aide la QA factuelle, mais peut nuire aux décisions séquentielles en détournant l’attention. Le code est annoncé pour après acceptation ; nous n’avons pas reproduit les résultats. C’est un argument pour choisir la stratégie selon la tâche et mesurer le coût total, pas pour acheter un composant particulier. [Prépublication](https://arxiv.org/abs/2608.15008).

**MemoryLACE**, soumis le **2 septembre 2026**, propose des relations clairsemées de fusion, remplacement et contradiction entre faits atomiques avec provenance. La reconstruction peut distinguer état actuel, historique et preuves en conflit. Les auteurs annoncent des gains sur BEAM/StructMemEval ; lecture du résumé et des métadonnées, pas reproduction indépendante. **À adapter expérimentalement dans SQLite**, où ces relations peuvent être représentées sans importer un moteur de graphe complet. Licence du code et coût réel d’ingestion à vérifier avant toute réutilisation. [Prépublication](https://arxiv.org/abs/2609.03201).

**The Compaction Cliff** constitue un signal supplémentaire : les auteurs étudient les pertes de règles lors de résumés répétés et proposent une politique de rétention selon le type d’information. L’extrait contient un pied de page parlant d’une conférence de **novembre 2026**, postérieure à notre date. Nous ne le présentons donc pas comme un article déjà paru à cette conférence. La date exacte de dépôt n’a pas été confirmée par la page extraite ; les scores ne servent pas de critère de livraison. À retenir comme hypothèse à tester : conserver les contraintes explicitement et vérifier leur survie à chaque compaction. [Prépublication consultée](https://arxiv.org/html/2608.22752v1).

TierMem, MegaMem, LMEB et LoCoMo-Plus ont été signalés ou découverts mais ne sont pas évalués en détail dans cet axe ; aucun résultat non lu n’est utilisé comme preuve.

## Échecs concrets et portée des témoignages

1. **Compactions redondantes Letta.** L’issue du 23 mars 2026 décrit une estimation fondée sur total entrée+sortie qui déclenche une deuxième compaction inutile et une compression de résumé déjà compressé. Elle donne des chemins de code et une reproduction ; cela reste un signalement utilisateur. La fermeture d’août provient du bot d’inactivité, **pas d’une preuve de correction**. Pour Swarmer : compter séparément entrée effective, sortie réservée et fenêtre réellement servie. [Issue 3242](https://github.com/letta-ai/letta/issues/3242).

2. **Pollution et réingestion Mem0.** Un utilisateur rapporte un audit de 10 134 entrées après 32 jours, avec doublons, confusion d’identité et souvenirs réinjectés puis réextraits. Son pourcentage spectaculaire n’est pas un taux d’erreur universel. Un contributeur répond le 5 juin que de nouvelles fonctions traitent temporalité, déduplication et décroissance, tout en reconnaissant des questions ouvertes. L’échange ne démontre ni indépendamment l’audit ni l’équivalence des offres OSS/hébergée. [Issue et réponse](https://github.com/mem0ai/mem0/issues/4573#issuecomment-4631344261).

3. **Suppression partielle.** La PR du 23 mars Mem0 traite un effacement vectoriel laissant des données dans le graphe. Son état fermé seul ne prouve pas le merge ni la version de livraison. Le défaut décrit motive un test d’effacement transversal et d’absence de réapparition après réindexation. [PR 4505](https://github.com/mem0ai/mem0/pull/4505).

4. **Résolution de graphe non bornée.** Une issue Graphiti du 26 février 2026, hors fenêtre récente mais utile, rapporte une croissance du contexte lors de la résolution d’entités et des échecs d’ingestion après retries. Aucun commentaire ne confirme le diagnostic. À traiter comme cas de stress à reproduire, pas comme défaut actuel établi : borner les candidats et rendre visible tout épisode rejeté. [Issue 1275](https://github.com/getzep/graphiti/issues/1275).

## Licences et activité vérifiées

Métadonnées publiques GitHub consultées le 22 septembre 2026. Elles prouvent l’état du dépôt observé, pas la compatibilité de toutes ses dépendances ni sa qualité opérationnelle.

| Dépôt | Licence déclarée | Dernier push observé | Décision locale |
|---|---|---|---|
| [letta-ai/letta](https://github.com/letta-ai/letta) | Apache-2.0 | 10 septembre 2026 | Adapter les blocs ; différer migration. |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | 21 septembre 2026 | Essai d’extraction isolé seulement après benchmark. |
| [getzep/graphiti](https://github.com/getzep/graphiti) | Apache-2.0 | 21 septembre 2026 | Adapter provenance/temporalité ; différer nouveau datastore. |
| [langchain-ai/langmem](https://github.com/langchain-ai/langmem) | MIT | 9 septembre 2026 | Concepts ou composants limités. |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | MIT | 21 septembre 2026 | Garder le runtime existant ; comparer interfaces si besoin. |

Les cinq dépôts sont déclarés non archivés et non désactivés. Un push récent ne suffit pas à établir un support garanti. Vérifier le commit, la licence du code effectivement incorporé et les dépendances au moment d’une adoption.

## Architecture minimale proposée pour Swarmer

1. **Journal canonique récupérable.** Événements et reçus immuables avec IDs ; références de fichiers et de révisions associées à leur hash. Distinguer déclaration du modèle, action proposée, action exécutée et résultat vérifié.
2. **État de reprise structuré et versionné.** Objectif courant, contraintes explicites, décisions, points ouverts, étape suivante, liens vers fichiers/reçus, et bornes de validité. Chaque fait conserve son origine. Un LLM peut proposer un patch ; une transaction valide son schéma, sa base et sa portée avant écriture.
3. **Contrainte et preuve avant narration.** Épingler les instructions en vigueur et les refus/retraits récents ; ne jamais convertir une note résumée en autorisation. Les résultats effectifs conservent leurs identifiants, statut et révision. Ne pas confondre préférence durable et état transitoire d’une tâche.
4. **Récupération limitée et explicable.** Filtrer d’abord par utilisateur/projet, type, temps et révision ; récupérer ensuite par identifiant, texte ou similarité. Enregistrer les IDs sélectionnés, le budget et les exclusions. Mesurer séparément le rappel de la bonne preuve et sa bonne utilisation par le modèle.
5. **Masquage avant synthèse coûteuse.** Alléger d’abord les anciennes observations longues et récupérables. Garder un reçu compact et une lecture ciblée possible. Conserver les contraintes sans paraphrase destructrice. Si une synthèse est nécessaire, la produire depuis les sources et un état validé, avec vérification explicite des contraintes survivantes.
6. **Consolidation asynchrone bornée.** Exclure du corpus d’extraction les souvenirs rappelés, messages système, bruit de heartbeat et secrets. Déduplication, remplacement et suppression portent sur des événements sources. Comptabiliser ces appels dans les budgets ; conflits d’écriture gérés par version/base, sans retries invisibles.

Cette proposition ne nécessite pas dix processus de mémoire autonomes. Plusieurs rôles peuvent partager un service de lecture et des données communes, avec des écritures contrôlées par un seul contrat. « Mémoire commune » ne signifie pas « toutes les données accessibles à tous les rôles ».

## Qualification à faire avant de choisir une solution

Comparer à modèles, contexte total et budgets constants : **A** sélection actuelle ; **B** état structuré + masquage ; **C** résumé LLM ; **D** état structuré + récupération et résumé ciblés. Les appels d’extraction, de résumé et de reranking comptent dans le coût total.

Le jeu de tests doit inclure cinq compactions successives, une reprise après plus de cent révisions, une préférence corrigée puis annulée, une autorisation retirée, deux agents écrivant simultanément, des exigences françaises anciennes, une question temporelle, une preuve de test appartenant à une ancienne révision, un souvenir sans source et une suppression suivie d’une réindexation. Les fixtures sont bénignes et les actions testées restent contrôlées.

Mesurer : rétention des contraintes critiques ; rappel de la bonne version et abstention si la preuve manque ; attribution correcte ; contradictions et doublons ; résultats réellement exécutés ; lectures supplémentaires ; mémoire disque ; jetons et latences p50/p95. Une préférence corrigée ne doit pas revenir par proximité vectorielle. Une réponse fluide ou un score LLM seul ne valide pas l’exécution.

**Critère proposé, non encore mesuré :** aucune perte ni réactivation d’une contrainte critique dans le jeu fixe, aucune promotion d’un récit du modèle en preuve, aucune fuite entre projets ; amélioration du taux de tâches terminées sous le même budget. N’ajouter un graphe ou un framework que si son gain incrémental dépasse celui de l’état structuré et de la récupération bornée.
