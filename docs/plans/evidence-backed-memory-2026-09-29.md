# Mémoire de travail et preuves versionnées — décision d’architecture

29 septembre 2026 UTC. Complément au registre de 57 travaux, intégrant le cadre fourni par l’utilisateur. Les états de réalisation sont ceux du code local ; **aucune activation en production n’est revendiquée**. Les références scientifiques et leurs limites sont vérifiées séparément dans [le relevé de recherche](../research/memory-literature-verification-2026-09-29.md).

## Décision

Conserver SQLite comme autorité derrière les services du control plane. Les agents distants utilisent l’API ; ils n’ouvrent pas le fichier SQLite à distance. Faire évoluer les services existants vers une interface commune de mémoire et d’état, avec projections lexicales, symboliques et vectorielles reconstruisibles. Une recherche retourne des candidats ; leur admission dans le contexte dépend aussi de leur portée, de leur version, de leur origine et du travail en cours.

Les consignes de confiance restent dans le chemin de configuration et le contrat actif du projet. Elles ne dépendent pas d’une similarité vectorielle. Les pages web, les fichiers du dépôt, les sorties d’outils et les souvenirs récupérés restent des données ; ils ne peuvent pas modifier les permissions.

Les épisodes déjà enregistrés ne sont pas promus rétroactivement en connaissances vérifiées. Un statut `completed`, une note d’évaluateur ou une phrase « les tests passent » ne prouve pas un test exécuté. Une preuve d’exécution reste limitée à sa commande, à ses artefacts, à son environnement et aux critères qu’elle couvre.

Exigence linguistique initiale : les souvenirs réutilisables utilisent un pivot anglais, après traduction vérifiée des sources françaises. La précision ultérieure de l’utilisateur conserve trois représentations d’une même identité : original, vue linguistique et concepts/claims. Le [plan multilingue symbolique](multilingual-symbolic-memory-2026-09-29.md) remplace donc la restriction initiale à un seul vecteur anglais : les vues originale et pivot peuvent avoir leurs propres projections, sans créer deux souvenirs. Le [contrat de normalisation anglaise](english-canonical-memory-2026-09-29.md) conserve ses exigences de littéraux protégés, ambiguïtés, révisions et absence de fusion fondée sur la seule traduction.

## Raccordement au produit existant

| Responsabilité | Points d’appui locaux | Suite nécessaire |
|---|---|---|
| Identité et état du projet | M01 : identité atomique, continuité de conversation et admission des reprises dans `project_identity.py` | Qualifier les anciennes lignées sur copie ; ne jamais fusionner deux projets ambigus. |
| Contraintes explicites | F01/F02 et contexte des objectifs : provenance des messages et critères de rédaction | M02/M06 : contrat versionné de demandes, décisions, corrections et questions ouvertes, lu avant toute recherche. |
| Connaissances du dépôt | Révisions, fichiers, hashes, guides AGENTS et contexte ciblé existants | M10 : index symboles/AST lié au snapshot, recherches exactes et lexicales, lecture par plages avant modification. |
| Expériences | Épisodes terminaux et reçus de jobs/outils/modèles existants | M09 : distinguer constat, interprétation et leçon candidate ; conserver les conditions et les contre-exemples. |
| Connaissances durables | Mémoire générale et projet, filtres de portée, déduplication, invalidation après correction | M02/M06/M09 : historique de versions, application conditionnelle et promotion explicite avec preuve. |
| Recherche | Fusion lexicale/sémantique et repli lexical localement renforcés | M04/M10/M12 : signature homogène, phase de travail, index de code, corpus d’évaluation et retard d’index visible. |
| Visibilité | M05 : consultation passive des reçus de récupération/appel/job implémentée localement | Catalogue unifié, correction, import et états de validité restent des parcours distincts. |

Le graphe de connaissances relie demande, exigence, décision, artefact, contrôle et expérience. Il complète le DAG d’exécution des agents ; une dépendance d’exécution ne signifie pas qu’une exigence a une preuve.

## Contrat de validité à introduire

Un enregistrement durable comporte : identité et version ; projet et namespace ; catégorie ; phase de travail ; contenu ; date observée ; références de sources ; snapshot ; empreintes des dépendances ; références de preuves ; résultat observé ; conditions d’application ; liens de remplacement ; état de cycle de vie. L’index conserve séparément sa révision de projection et sa signature d’embeddings.

Trois axes restent indépendants :

| Axe | Exemples | Règle |
|---|---|---|
| Origine et preuve | demande utilisateur, affirmation assistant, reçu d’outil, exécution de test observée | Le type est établi par une relation autoritaire ; le modèle ne s’auto-attribue pas le statut vérifié. |
| Applicabilité | snapshot exact, dépendances inchangées, historique seulement, revalidation requise | Une preuve ancienne peut rester informative sans devenir une preuve du code courant. |
| Cycle de vie | candidat, accepté, en quarantaine, remplacé, retiré | Un résultat d’index ne réactive pas une version retirée ou interdite. |

Le snapshot d’une preuve inclut le commit de base lorsqu’il existe, les contenus réels des fichiers concernés, les modifications non commitées, les dépendances et la configuration pertinente. Une branche ou un numéro de ligne ne suffit pas. Un hash identifie un contenu ; il ne prouve ni la vérité d’une affirmation ni son autorisation.

Le lecteur retourne l’applicabilité avec les références vérifiables. Il ne rejette pas toute expérience d’un autre commit : une procédure historique peut être proposée avec ses préconditions. En revanche, la clôture d’une exigence exige une preuve encore applicable à la révision évaluée.

## Pipeline de récupération

1. Autoriser le projet, le namespace et la portée ; charger les contraintes actives obligatoires.
2. Déterminer l’intention et la phase explicite : reproduction, localisation, modification, récupération après échec ou validation.
3. Chercher les identifiants/chemins exacts, puis lexicalement et sémantiquement dans les projections autorisées. Conserver les identifiants originaux et leurs formes segmentées.
4. Fusionner les rangs, dédupliquer les occurrences et diversifier les candidats. RRF constitue la référence initiale ; les scores des canaux ne sont pas sommés directement.
5. Étendre quelques relations de code ou de preuve seulement si elles sont utiles et autorisées. Chaque expansion applique les mêmes frontières de projet.
6. Relire l’enregistrement canonique et vérifier les versions, dépendances et règles d’application.
7. Construire le contexte borné et son reçu : versions transmises, motifs de sélection, éléments omis, modes de repli et budget. Résoudre les références vers le code courant avant de l’éditer.

La reproduction privilégie symptômes et conditions ; la localisation privilégie symboles, appelants et tests ; la modification privilégie décisions et patterns applicables ; la récupération privilégie tentatives échouées et contre-exemples ; la validation privilégie critères d’acceptation et limites des contrôles antérieurs.

Le résultat doit distinguer absence de souvenir applicable, index en retard, configuration d’embeddings incompatible, service indisponible et accès refusé. Un repli lexical réussi ne cache pas l’indisponibilité du canal sémantique. L’API actuelle M05 montre des reçus historiques ; elle ne prétend pas fournir encore tous ces nouveaux diagnostics.

## Écriture, projection et effacement

L’agent soumet une observation ou une leçon candidate avec ses sources. Le service vérifie l’identité, la portée, la version attendue et les preuves ; il accepte la révision ou conserve une proposition en quarantaine. Aucune extraction automatique ne modifie les règles de confiance.

Pour une projection asynchrone, enregistrer la révision canonique et un événement d’indexation dans la même transaction. Le projecteur traite cet outbox de façon idempotente ; il ne peut pas publier une ancienne révision après une correction. Enregistrer le watermark et les erreurs sans les transformer en collection vide. Un futur backend Qdrant suivrait ce même contrat.

À la lecture, revérifier le statut canonique et la politique d’accès, même si la projection n’a pas encore reçu une suppression. L’effacement retire le contenu et ses dérivés conformément à la politique retenue ; l’historique de versions n’autorise pas à conserver un secret ou une donnée dont l’effacement a été demandé. Les occurrences utiles à l’audit restent séparées du corpus proposé aux modèles.

## Technologies et modèles

La référence initiale conserve les vecteurs SQLite existants et introduit la recherche de symboles/FTS5 là où elle est mesurable. Un ajout de sqlite-vec, PostgreSQL/pgvector ou Qdrant dépend d’un besoin de volume, latence, filtrage ou exploitation démontré. Le coût d’un service supplémentaire entre dans la comparaison. SQLite WAL nécessite des processus sur le même hôte et n’est pas un partage de fichier distribué. [Documentation SQLite](https://sqlite.org/wal.html).

Qdrant fournit des requêtes hybrides et une fusion de rangs ; cela ne remplace pas la vérification canonique proposée ici. sqlite-vec avertit encore de changements possibles avant la version 1. Les scans itératifs pgvector peuvent améliorer les résultats filtrés approximatifs sans garantir un rappel exact. [Qdrant](https://qdrant.tech/documentation/search/hybrid-queries/), [sqlite-vec](https://github.com/asg017/sqlite-vec), [pgvector](https://github.com/pgvector/pgvector).

Deux candidats restent à comparer en environnement isolé : Qwen3-Embedding-0.6B pour mémoire/documentation et C2LLM-0.5B pour le code. La carte officielle Qwen annonce des dimensions de 32 à 1 024 et des instructions de requête ; les exemples C2LLM chargent du code personnalisé avec `trust_remote_code=True`. Une révision auditée et épinglée est donc nécessaire avant exécution. Aucun téléchargement ni remplacement du modèle actif n’est réalisé par ce document. [Qwen](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), [C2LLM](https://huggingface.co/codefuse-ai/C2LLM-0.5B).

La signature cible couvre modèle/révision, dimensions réelles, templates effectivement appliqués, normalisation, pooling et version du découpage. Deux vecteurs de même taille ne sont pas nécessairement comparables. Une révision inconnue reste inconnue : un hash de configuration ne prouve pas l’identité des poids servis derrière un alias mutable. Recréer une projection, la qualifier, puis choisir explicitement sa version active lors d’un changement de modèle.

## Lots et critères de passage

| Ordre | Lot du registre | Livrable | Critères exécutables |
|---|---|---|---|
| 1 | M01/M03/M04/M05/M11/M12 | Identité stable, portée, déduplication, signatures et inspection | Aucun mélange entre projets ou espaces de vecteurs ; correction concurrente non réinjectée ; GET sans mutation ; reçus historiques distincts des sources courantes. |
| 2 | M02/M06/M09 | Enregistrements versionnés et validation des preuves | « test réussi » annoncé sans reçu reste une affirmation ; changement d’artefact invalide la preuve courante ; correction utilisateur remplace l’ancienne instruction sans l’effacer de l’audit autorisé. |
| 3 | M10/M12 | Index symbolique/lexical de révisions et projection reprenable | Identifiant exact retrouvé ; fichier non commité pris en compte ; panne/rejeu du projecteur idempotent ; aucune lecture de code d’une révision différente présentée comme courante. |
| 4 | M04/M07/M10 | Recherche hybride par phase et contexte borné | Contraintes protégées conservées, doublons retirés, références résolues avant édition ; même requête peut sélectionner des preuves différentes selon la phase ; aucun dépassement silencieux du budget. |
| 5 | M08/M09/M11 | Imports et procédures candidates | URL/PDF/TXT correctement sourcés ; promotion conditionnelle explicite ; contre-exemple conservé ; suppression et accès révoqué propagés à toutes les projections. |
| 6 | M04/M09/M10 | Comparaison contrôlée et choix des options | Gains mesurés sur tâches et non seulement similarité ; aucun changement de moteur, reranker ou modèle sur simple classement externe. |

## Protocole de mesure

Comparer A (consignes fiables + recherche ordinaire), B (A + sémantique), C (A + hybride), D (C + expériences versionnées), E (D + procédures validées). Conserver le même modèle de code, les mêmes outils, les mêmes tâches et le même budget total. Les bascules sont explicites ; aucune branche du harnais ne reçoit des solutions ou commits futurs.

Corpus bénin initial : recherche de symbole exact ; erreur littérale ; question conceptuelle ; nom de fonction renommé ; branche divergente ; modification non commitée ; dépendance mise à jour ; critère de test non exécuté ; ancien test devenu périmé ; décision utilisateur inversée ; suppression ; mémoire d’un autre projet ; source contenant de fausses instructions ; index en retard ; interruption et reprise d’indexation. Ajouter des tâches de correction avec contrôles externes à l’agent.

Mesurer rappel à profondeur fixée et exactitude des identifiants, réussite réelle des tâches, régressions, répétition d’échecs, influence négative d’anciens souvenirs, utilisation de preuves périmées, fuites de portée, latence totale, tokens et retard d’indexation. Rapporter les échecs et interruptions, pas seulement les moyennes des succès. Le reranking et les expansions de graphe font chacun l’objet d’une ablation ; aucun gain publié n’est transféré automatiquement à monGARS.

## Activation

Développer et qualifier sur des bases temporaires, puis reconstruire une projection issue d’une copie cohérente en mode observation. Comparer les contextes sans influencer les projets utilisateur. L’activation de nouveaux lecteurs et écritures suit une livraison traçable avec compatibilité de schéma ; le retour arrière conserve les écritures récentes. La qualification iPhone et l’usage réel restent des preuves séparées des tests locaux.
