# Spécialistes SQLite, Swift/iOS et Python — recherche du 22 septembre 2026

## Conclusion applicable à Swarmer

**Ajouter des capacités exécutables et vérifiables avant d’ajouter des noms d’agents.** Priorité : un spécialiste SQLite qui inspecte et valide sur copie, un spécialiste Python branché au runner existant, puis un worker Swift/iOS sur l’iMac. Chaque rôle doit publier les outils réellement disponibles, leurs prérequis et les preuves qu’ils produisent.

Contexte local vérifié en lecture : `project_contracts.py` accepte actuellement seulement `python`, `node`, `python_node`. Le README du project-worker décrit déjà des commandes fixes, des conteneurs sans réseau pendant les tests, des dépendances filtrées, des limites de ressources et l’annulation sur perte de lease. Il faut conserver ces protections. Xcode **26.3 build 17C529** sur l’iMac est une mesure transmise par le parent ; cette sous-tâche ne l’a pas remesurée.

Les noms de capacités ci-dessous sont **des propositions d’intégration**, pas des outils déjà enregistrés ou des agents actuellement disponibles.

## 1. Spécialiste SQLite : recommander maintenant

L’adaptateur initial peut utiliser `sqlite3` de Python : inspection du schéma, requêtes bornées en lecture, sauvegarde cohérente, préparation et vérification des migrations. Le modèle propose l’opération ; l’API choisit la base autorisée et applique un contrat. Les chemins de base, extensions ou commandes arbitraires ne doivent pas provenir directement du modèle.

L’authorizer de SQLite décide pendant la préparation des instructions, peut refuser des opérations et doit rester installé lorsqu’une requête peut être recompilée. Il ne limite pas le coût du calcul : ajouter des limites de durée, lignes et octets. Un résultat « statement readonly » ne constitue pas un confinement complet, notamment pour les transactions et bases attachées. [Authorizer SQLite](https://sqlite.org/c3ref/set_authorizer.html), [limites de stmt_readonly](https://www.sqlite.org/c3ref/stmt_readonly.html).

Pour une sauvegarde d’une base utilisée, préférer l’API de sauvegarde à une copie naïve du seul fichier principal. L’API produit un instantané cohérent ; des écritures concurrentes peuvent toutefois faire recommencer la copie, donc une limite de temps demeure nécessaire. `BEGIN IMMEDIATE` réserve l’écriture et peut échouer avec `SQLITE_BUSY` ; il ne faut pas transformer cela en boucle illimitée. [Sauvegarde SQLite](https://www.sqlite.org/backup.html), [transactions](https://www.sqlite.org/lang_transaction.html), [isolation WAL](https://www.sqlite.org/isolation.html).

| Capacité proposée | Entrée contrôlée | Preuve utile |
|---|---|---|
| `database.inspect` | Base autorisée et projection de tables | Version du schéma, colonnes/index/relations ; sortie expurgée |
| `database.query_readonly` | SQL paramétré, bornes strictes | Durée, lignes, troncature, refus explicites d’écriture |
| `database.backup` | Destination privée gérée par le serveur | Hash de la copie, contrôle d’intégrité, instant de capture |
| `database.migration_validate` | Patch et copie de test | Diff de schéma, contraintes, intégrité, retour arrière |
| `database.migration_apply` | Révision approuvée, préconditions exactes | Transaction, version avant/après, audit canonique |

**Tests d’acceptation** : refuser `ATTACH`, écritures, PRAGMA de mutation et chargement d’extensions dans le profil de lecture ; interrompre une requête coûteuse ; vérifier les bornes de résultat ; sauvegarder sous écritures WAL ; préserver clés étrangères, index et déclencheurs pendant une migration ; prouver qu’un échec ne laisse aucune modification partielle. Ce sont des critères proposés, pas des tests exécutés ici.

### sqlite-utils, Datasette et Alembic

**Adapter sqlite-utils pour les imports/exports de copies.** Sa documentation montre que `db.query()` peut exécuter et valider une écriture `INSERT ... RETURNING`. Certaines PRAGMA prennent effet même si l’appel produit ensuite une erreur ; les méthodes d’écriture valident automatiquement leur propre transaction. Ce n’est donc pas une frontière de lecture seule et ce n’est pas un remplacement transparent du gestionnaire transactionnel de Swarmer. Les plugins de connexion sont aussi actifs par défaut ; une intégration contrôlée doit les désactiver ou les inventorier. [API sqlite-utils](https://sqlite-utils.datasette.io/en/stable/python-api.html).

**Différer Datasette pour le contrôle opérationnel ; l’adapter pour consulter un corpus choisi.** Il fournit une interface d’exploration et une API de publication de données. Une copie expurgée peut être utile aux recherches personnelles ou administratives ; ouvrir toute la base interne créerait une deuxième surface d’accès inutile. [README amont Datasette](https://github.com/simonw/datasette/blob/main/README.md).

**Différer Alembic pour la base interne existante.** Il est pertinent pour les nouveaux projets Python utilisant SQLAlchemy. Sa migration SQLite en mode batch reconstruit les tables par copie ; les CHECK non nommés et certaines clés étrangères demandent un traitement explicite. Une migration générée doit être exécutée et vérifiée sur copie, pas tenue pour correcte parce que le code a été produit. [Batch SQLite Alembic](https://alembic.sqlalchemy.org/en/latest/batch.html).

## 2. SQLite vectoriel : pilote sqlite-vec, pas de promesse de mémoire active

**Adapter sqlite-vec en pilote, différer sqlite-vss.** Le mainteneur de sqlite-vss indique que ses efforts vont à sqlite-vec. Les métadonnées GitHub confirment un dernier push de sqlite-vss en mai 2024, même si le dépôt n’est pas archivé. sqlite-vec est en C, vise plusieurs environnements dont mobiles, et demeure pré-v1 : la compatibilité doit être testée et la version épinglée. [sqlite-vss](https://github.com/asg017/sqlite-vss), [sqlite-vec](https://github.com/asg017/sqlite-vec).

Le dernier tag stable vérifié par l’API GitHub est **sqlite-vec 0.1.9**, publié le 31 mars 2026. Les 0.1.10 alpha de mars à mai introduisent des travaux ANN ; ne pas sélectionner une alpha seulement parce qu’elle est plus récente. [Version stable](https://github.com/asg017/sqlite-vec/releases/tag/v0.1.9), [versions du mainteneur](https://github.com/asg017/sqlite-vec/releases).

Un index vectoriel n’est ni un modèle d’embedding ni une mémoire partagée à lui seul. Pour prouver la mémoire sémantique, exiger : modèle/révision d’embedding, dimension, métrique, texte source et provenance, réception de l’écriture, récupération effective, puis utilisation des passages dans l’appel suivant. iPhone et Ubuntu doivent échanger un contrat versionné ; partager un fichier SQLite vivant par réseau n’est pas la synchronisation proposée.

**Tests du pilote** : incompatibilité de dimensions rejetée ; voisins attendus sur un petit corpus français ; suppression et réindexation exactes ; filtres d’accès au projet appliqués ; mêmes identifiants de provenance entre appareils ; latence, RAM et persistance iPhone mesurées. La qualification du modèle d’embedding est traitée séparément par le parent.

## 3. Swift/iOS : exécution iMac et reçus natifs

### XcodeBuildMCP : adapter derrière le worker, version épinglée

La version stable actuelle vérifiée est **2.7.0**, publiée le 23 juillet 2026. Le README demande macOS 14.5+, Xcode 16+ et Node 18+ pour la distribution Node ; la version Xcode rapportée par le parent dépasse ce minimum, sans prouver une compilation du projet. [Version](https://github.com/getsentry/XcodeBuildMCP/releases/tag/v2.7.0), [README](https://github.com/getsentry/XcodeBuildMCP/blob/main/README.md).

Le catalogue expose réellement `build_device`, `test_device`, `test_sim`, les commandes SwiftPM et des lectures de couverture `xcresult`. **Son groupe d’automatisation UI concerne le simulateur** : il ne faut pas en déduire que les clics sur iPhone physique sont disponibles. Une autre subtilité utile : `stop_device_log_cap` est documenté comme arrêtant aussi l’app ; un adaptateur ne doit pas présenter cette action comme une simple lecture de journaux. [Catalogue des outils](https://github.com/getsentry/XcodeBuildMCP/blob/main/docs/TOOLS.md).

Apple propose aussi un pont MCP officiel, `xcrun mcpbridge`, avec autorisation dans les réglages Intelligence et un projet ouvert dans Xcode. Le choix entre ce pont et XcodeBuildMCP doit suivre la découverte réelle des capacités de l’Xcode installé ; pas une supposition fondée sur des articles Xcode 27 beta. [Documentation Apple](https://developer.apple.com/documentation/xcode/giving-agentic-coding-tools-access-to-xcode).

**Intégration minimale** :

1. Enregistrer un worker iMac authentifié avec inventaire Xcode/SDK/destinations et outils sélectionnés.
2. Recevoir un job canonique `ios.build`, `ios.test` ou `swift.package_test` avec source figée, scheme, destination et durée maximale.
3. Exécuter dans un checkout isolé ; conserver les actions d’installation/lancement distinctes des tests de compilation.
4. Retourner exit code, nombre de tests réellement exécutés, plateforme, version Xcode, hash de source, journaux bornés et hash/emplacement du `.xcresult`.
5. Supprimer du catalogue disponible les capacités dont l’hôte, le tunnel ou la destination ne répondent plus.

**Tests nécessaires** : package Swift volontairement invalide ; test qui échoue ; zéro test non assimilé à une réussite fonctionnelle ; destination absente ; annulation et lease périmée ; distinction résultat simulateur/appareil physique ; reçu refusé s’il correspond à une autre source. La compilation iOS sur Ubuntu n’est pas proposée.

### GRDB.swift : adapter si un module natif possède sa base

GRDB **7.11.1**, publié le 18 juin, fournit observation, requêtes typées, migrations et accès concurrents. Son README indique Swift 6.1+/Xcode 16.3+ et iOS 13+. `DatabaseQueue` simplifie la sérialisation ; `DatabasePool` ouvre WAL et autorise des accès concurrents. [README GRDB](https://github.com/groue/GRDB.swift/blob/master/README.md), [version vérifiée](https://github.com/groue/GRDB.swift/releases/tag/v7.11.1).

C’est un bon candidat pour un nouveau module natif de persistance ou de recherche, **pas une raison de réécrire la base Expo ni de créer deux propriétaires de schéma**. Tester migrations successives, concurrence et disponibilité des données quand l’iPhone est verrouillé ; ne pas confondre un refus lié à la protection des fichiers avec une panne du modèle.

## 4. Python : améliorer l’outillage existant avant un nouveau moteur

**Recommander des profils déterministes Ruff + pytest**, avec interpréteur et versions enregistrés. Ruff fournit lint et formatage ; la vérification doit rester distincte d’une correction automatique. pytest permet sélection, collecte et exécution ciblées ; sa documentation déconseille les répétitions de `pytest.main()` dans un même processus car les imports peuvent être mis en cache. Utiliser un processus neuf pour chaque preuve. [Ruff](https://github.com/astral-sh/ruff/blob/main/README.md), [pytest](https://docs.pytest.org/en/stable/how-to/usage.html).

**Adapter uv lorsque le format de projet le justifie.** Son lockfile stocke les versions résolues, `uv run` synchronise l’environnement et `uv build` produit des distributions. Cela n’autorise pas à ouvrir le réseau ou les sources Git dans le runner actuel : conserver les politiques de wheels, versions et registres autorisés. Ne pas remplacer automatiquement tous les requirements existants. [Projets uv](https://docs.astral.sh/uv/guides/projects/).

**Adapter les contrats typés de PydanticAI, différer un second ordonnanceur.** La documentation actuelle présente outils, tests et un harness de capacités. C’est une source d’idées pour valider entrées/sorties et tester les erreurs ; son adoption complète n’apporterait pas à elle seule les leases, historiques et règles de facturation propres à Swarmer. [Dépôt PydanticAI](https://github.com/pydantic/pydantic-ai), [tests officiels](https://pydantic.dev/docs/ai/guides/testing/).

**Épreuve utile** : CRM Python minimal avec base SQLite et tests CRUD réels, puis requête de modification ; exiger que le second reçu corresponde au nouveau contenu. Garder les cas « tests absents », dépendance refusée, limite de temps, action de lecture répétée et annulation. La réussite de tests écrits par le modèle demeure une preuve limitée de fonctionnement.

## 5. Développeur avancé : deux pilotes possibles, pas d’intégration globale immédiate

| Option | Ce qui est établi | Décision pour Swarmer |
|---|---|---|
| OpenHands Software Agent SDK | API Python et AgentServer HTTP/WebSocket ; commandes et fichiers via workspace ; option Docker | **Adapter en pilote** pour une boucle d’édition outillée ; un workspace local n’est pas automatiquement isolé |
| Aider | Boucle lint/test et commandes de tests configurables | **Adapter en pilote** pour petits correctifs ; borner chaque appel, tentative et commande depuis le worker |
| SWE-agent historique | Le README recommande désormais mini-swe-agent | **Différer** ; analyser le successeur séparément avant de choisir |

Sources : [OpenHands AgentServer](https://docs.openhands.dev/sdk/guides/agent-server/overview), [Aider lint/test](https://aider.chat/docs/usage/lint-test.html), [recommandation des mainteneurs SWE-agent](https://github.com/SWE-agent/SWE-agent/blob/main/README.md).

Le pilote doit comparer ces moteurs à l’existant avec **le même modèle local et les mêmes budgets**, sur des tâches bénignes reproductibles : réparer un test, ajouter une migration, créer une route CRM, corriger une erreur Swift. Aucun score externe n’est transposé aux modèles disponibles ici. Tous les sous-appels doivent rester visibles et facturés ; une boucle interne ne doit pas dissimuler vingt générations derrière un job annoncé comme un seul appel.

## 6. Preuve commune et ordre d’implémentation proposé

Le reçu canonique devrait relier `goal/task/node/job`, génération de lease, outil/version, arguments expurgés, source et configuration, horodatage/durée, statut, exit code, tests exécutés/échoués/ignorés et hashes d’artefacts. Les déclarations rédigées par le modèle ne remplacent pas les faits du runner. Une inspection n’est pas une modification ; une compilation n’est pas un test sur appareil ; un test n’est pas un déploiement.

1. **SQLite + Python** : adaptateurs bornés autour du runtime existant ; exposer les vrais appels et reçus dans l’API.
2. **Swift/iOS iMac** : disponibilité et compilation/test sans manipulation d’écran.
3. **GRDB et sqlite-vec** : preuves sur module/corpus isolé avant migration.
4. **Moteur de développement avancé** : choisir après le pilote comparatif ; préserver l’API et l’ordonnanceur existants.

## Licences et maintenance vérifiées

Métadonnées GitHub consultées le 22 septembre. Le dernier push indique une activité du dépôt, **pas** une certification de qualité ou la date d’une version stable. Tous ces dépôts étaient non archivés ; cela ne contredit pas l’avis de maintenance inactive de sqlite-vss.

| Dépôt | Licence déclarée/détectée | Dernier push UTC |
|---|---|---|
| [XcodeBuildMCP](https://api.github.com/repos/getsentry/XcodeBuildMCP) | MIT | 2026-09-11 |
| [GRDB.swift](https://api.github.com/repos/groue/GRDB.swift) | MIT | 2026-09-15 |
| [sqlite-vec](https://api.github.com/repos/asg017/sqlite-vec) | Apache-2.0 | 2026-05-18 |
| [sqlite-vss](https://api.github.com/repos/asg017/sqlite-vss) | MIT | 2024-05-05 |
| [sqlite-utils](https://api.github.com/repos/simonw/sqlite-utils) | Apache-2.0 | 2026-09-02 |
| [Datasette](https://api.github.com/repos/simonw/datasette) | Apache-2.0 | 2026-09-21 |
| [Alembic](https://api.github.com/repos/sqlalchemy/alembic) | MIT | 2026-09-18 |
| [uv](https://github.com/astral-sh/uv/blob/main/README.md) | Apache-2.0 **ou** MIT, choix explicite du README | 2026-09-22 |
| [Ruff](https://api.github.com/repos/astral-sh/ruff) | MIT | 2026-09-22 |
| [pytest](https://api.github.com/repos/pytest-dev/pytest) | MIT | 2026-09-21 |
| [PydanticAI](https://api.github.com/repos/pydantic/pydantic-ai) | MIT | 2026-09-22 |
| [OpenHands SDK](https://api.github.com/repos/OpenHands/software-agent-sdk) | MIT pour ce dépôt SDK | 2026-09-22 |
| [Aider](https://api.github.com/repos/Aider-AI/aider) | Apache-2.0 | 2026-05-22 |
| [SWE-agent](https://api.github.com/repos/SWE-agent/SWE-agent) | MIT | 2026-09-21 |

## Portée de la recherche

Fenêtre récente : **22 mars–22 septembre 2026** ; documentation fondamentale plus ancienne conservée. Six requêtes Exa de dix résultats : **60 emplacements de résultats**, tous retournés, et 60 URL exactes distinctes, avec plusieurs variantes d’un même dépôt. Ce n’est pas 60 revues intégrales de sources indépendantes.

Dix pages ont été récupérées par Exa en un appel groupé. Six lectures GitHub de README complètent les extraits, dont GRDB dont l’extraction Exa était incomplète. Quatorze dépôts ont été découverts via le connecteur, puis leurs métadonnées publiques et trois versions stables ont été contrôlées. Les blogs, agrégateurs et comparatifs non primaires renvoyés par la recherche ne fondent pas les recommandations.

Le JSON associé conserve requêtes, compteurs, URL, dates rapportées, pages récupérées, licences, dates d’activité et décisions. Aucun dépôt téléchargé n’a été exécuté ; aucun outil installé ; aucune compilation, inférence ou mutation de service effectuée. Les propositions et critères de test sont une synthèse d’intégration, pas une annonce de fonctionnalités livrées.
