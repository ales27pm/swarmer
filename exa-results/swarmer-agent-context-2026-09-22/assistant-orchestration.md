# Assistant personnel, outils et orchestration — recherche du 22 septembre 2026

## Conclusion

Conserver l’API canonique de Swarmer, ses tâches, son DAG, ses baux, ses approbations et ses preuves SQLite. Ajouter des adaptateurs limités pour la recherche, les documents, l’agenda et le CRM, puis exposer ces capacités à des rôles d’assistant personnel. La présence d’un rôle, d’un modèle ou d’un serveur MCP ne prouve pas qu’une opération a été exécutée. Chaque capacité doit aboutir à un résultat validé, attaché à une tâche et à l’identité de son exécutant.

MCP est utile pour découvrir et appeler des outils structurés ; A2A sert à dialoguer avec des agents externes qui possèdent leur propre cycle de tâche. LangGraph et PydanticAI apportent des patterns intéressants, mais cette collecte ne justifie pas de remplacer le moteur durable déjà présent. Ce choix architectural est une recommandation appliquée au contexte transmis, pas le résultat d’un benchmark comparatif.

## Périmètre et méthode

Fenêtre récente : **22 mars au 22 septembre 2026**, complétée par les spécifications et incidents fondamentaux plus anciens. Recherche effectuée avec Exa : **6 recherches, 10 résultats demandés chacune, 60 emplacements demandés et 60 URL retournées**. Dix URL ont ensuite été récupérées dans deux appels : neuf pages utiles et une notice de déplacement OpenTelemetry. Dix-neuf sources ont été retenues. Ces nombres décrivent des emplacements et des récupérations ; ils ne signifient pas que 60 sources ont été lues intégralement.

Les requêtes, toutes les URL retournées, les récupérations, les dates connues et les limites sont consignées dans [assistant-orchestration-sources.json](assistant-orchestration-sources.json). Aucune installation, inférence, connexion à la production ou modification du runtime n’a été réalisée dans ce volet.

Le contexte existant ci-dessous vient de la mission et des travaux précédents ; il n’a pas été revérifié en production pendant cette recherche.

| Statut | Éléments |
|---|---|
| Déjà intégrés selon le contexte fourni | API canonique ; SQLite ; tâches et DAG ; baux et validation des résultats ; travailleurs code/projet, texte, fichiers, revue et recherche SearXNG ; contrôle des écritures/approbations. |
| Proposés dans cette note | Adaptateur MCP, extraction documentaire spécialisée, calendrier/CRM, automatisations métier, instrumentation et évaluations transversales décrites ci-dessous. |
| Non démontrés par cette recherche | Connexion effective Google Calendar/HubSpot/CalDAV ; actions métier distantes ; comportement sous panne de ces connecteurs ; performances de nouveaux frameworks ou extracteurs. |

## Choix techniques

| Sujet | Décision proposée | Fondement et limite |
|---|---|---|
| MCP | **Adapter maintenant**, après contrat et tests d’un premier serveur utile. | Découverte paginée, schémas et résultats structurés ; le protocole ne remplace pas les autorisations ni la preuve d’effet. |
| A2A | **Différer**, sauf agent exploité indépendamment qui impose ce protocole. | AgentCard, tâches, états et artefacts sont utiles à l’interopérabilité, mais ne rendent pas automatiquement une opération métier fiable. |
| LangGraph | **Réutiliser les patterns**, sans migration du moteur. | Checkpoints et séparation mémoire de thread/store ; risque de deux autorités concurrentes pour une même tâche. |
| PydanticAI | **Réutiliser les frontières typées** ; intégration optionnelle et isolée seulement si besoin prouvé. | Les moteurs durables proposés ajoutent leurs propres dépendances et modalités de reprise. |
| MarkItDown / Docling | **Qualifier un extracteur simple**, puis Docling pour scans/tableaux si nécessaire. | Le texte Markdown ne garantit ni fidélité visuelle ni ordre de lecture correct. |
| SearXNG | **Compléter le parcours existant** par récupération de pages et provenance. | Un résultat de recherche et son extrait ne constituent pas la preuve complète d’une affirmation. |
| n8n | **Pont interne optionnel**, avec opérations bornées. | Licence source-available ; certains usages embarqués avec identifiants de clients nécessitent un autre accord. |
| OpenTelemetry / OpenInference | **Instrumenter les opérations canoniques**. | Garder des identifiants stables et exporter des métadonnées filtrées. |
| Langfuse / Phoenix | **Choisir un seul backend de traces** après un petit essai. | Éviter deux piles parallèles et la duplication des données privées. |
| promptfoo | **Adapter pour les tests hors production**. | Les assertions de trajectoire complètent les tests fonctionnels ; elles ne prouvent pas seules la qualité du résultat. |

### MCP : découverte progressive et résultat réel

La [spécification MCP des outils, version 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) définit `tools/list`, sa pagination, les notifications de changement, `inputSchema`, `outputSchema` et `structuredContent`. Lorsqu’un schéma de sortie existe, le serveur doit le respecter et le client devrait valider le résultat.

Pour Swarmer, proposer un petit registre par domaine — recherche, documents, agenda, CRM, code — puis fournir au modèle seulement les outils pertinents et actuellement disponibles. Cette sélection progressive est un **design proposé**, pas une garantie automatique du protocole. Mettre en cache la découverte par serveur et version, avec invalidation explicite ; ne pas refaire l’inventaire complet à chaque tour.

Conserver `content` et `structuredContent` séparément. Vérifier nom, paramètres, taille, schéma de sortie, identité du serveur et droit effectif avant admission. Les annotations d’un outil et ses descriptions restent des métadonnées non fiables pour accorder une permission. Le catalogue devrait distinguer « décrit », « connecté », « autorisé », « exécuté et qualifié ». Un résultat MCP doit traverser la même validation que celui d’un worker natif.

Le [SDK Python MCP v2.0.0a3](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0a3) est explicitement une préversion. Son existence ne justifie pas de basculer sur une version alpha ; choisir une version stable épinglée et tester les capacités effectivement utilisées.

### A2A et durabilité : éviter deux moteurs de vérité

La [spécification A2A](https://a2a-protocol.org/latest/specification/) distingue AgentCard, Message, Task et Artifact. Une réponse peut être un simple message plutôt qu’une tâche suivie. Un état externe « terminé » doit donc être traduit en preuve locale vérifiée, avec correspondance entre identifiants, principal, annulation et artefacts. A2A devient pertinent pour un agent tiers autonome ; les workers internes n’en ont pas besoin pour être de vrais exécutants.

La page [LangGraph durable execution, qui retourne ici la documentation Persistence](https://docs.langchain.com/oss/python/langgraph/durable-execution) distingue checkpoints liés au thread et store transversal. Elle montre aussi que le stockage en mémoire n’assure pas la survie à un redémarrage. Les patterns sont transposables, mais aucune mesure ne montre qu’une migration améliorerait Swarmer.

[PydanticAI documente plusieurs intégrations durables](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/) : Temporal, DBOS, Prefect, Restate et AWS Lambda durable functions, entre autres. Pour le besoin actuel, des types Pydantic aux frontières d’outils et des reçus persistés apportent une amélioration plus ciblée qu’un second ordonnanceur.

La reprise d’un workflow ne rend pas une écriture distante atomique. Le [signalement LangGraph sur les limites de reprise et l’idempotence](https://github.com/langchain-ai/langgraph/issues/8702) motive un test explicite : après un délai réseau ambigu, rechercher l’opération par identifiant d’idempotence avant de la réémettre. L’état et la date exacts de ce signalement n’ont pas pu être établis dans la collecte ; il est utilisé comme retour d’expérience, pas comme garantie de correction publiée.

### Documents et recherche

[MarkItDown](https://github.com/microsoft/markitdown) vise une conversion légère en Markdown utile aux LLM, sans promettre une reproduction humaine de haute fidélité. Le projet propose des dépendances optionnelles et des plugins. Certains chemins ou enrichissements peuvent accéder au réseau ou à un service distant : « bibliothèque installée localement » ne signifie donc pas « traitement toujours hors ligne ».

Commencer par `documents.extract` sur un fichier déjà identifié et autorisé : format, taille et nombre de pages bornés ; extraction en processus isolé ; plugins et enrichissement cloud désactivés par défaut ; digest de fichier, version de l’extracteur et avertissements persistés. Les outils ne doivent pas recevoir un chemin arbitraire choisi par le modèle.

[Docling](https://github.com/docling-project/docling) fournit une représentation documentaire structurée, des fonctions d’OCR et de compréhension de tableaux/mise en page, avec export JSON ou Markdown. Son code est MIT, mais les licences des modèles sont distinctes. Le qualifier sur un corpus de factures, PDF à colonnes et scans québécois avant de le généraliser. Les coûts RAM, latence, précision OCR et fidélité des tableaux restent à mesurer.

L’[API de recherche SearXNG](https://github.com/searxng/searxng/blob/b3e08f2a/docs/dev/search_api.rst) fournit la découverte de sources. Pour répondre sur un horaire ou un service, ajouter une étape de lecture des pages retenues, conserver URL exacte, date de récupération, extrait et localisation dans le document. Une citation valide et une affirmation soutenue sont deux contrôles différents : un lien autorisé peut encore être associé à une affirmation erronée.

## Rôles concrets d’assistant personnel

Un rôle décrit une responsabilité ; une compétence décrit une opération exécutable. Plusieurs rôles peuvent partager le même modèle, et un modèle disponible ne rend pas disponible un connecteur.

| Rôle proposé | Compétences et résultat attendu | Contrôle d’acceptation essentiel |
|---|---|---|
| Coordinateur personnel | Comprendre la demande, choisir les compétences, répondre directement si aucun outil n’est nécessaire. | Une question d’agenda ou un résumé ne part pas en génération de projet logiciel. |
| Recherchiste | `research.query`, lecture des sources retenues, dossier de preuves. | Sujet et lieu correspondent à la demande ; fraîcheur explicite ; pages lues distinguées des extraits. |
| Rédacteur | Brouillon, résumé ou courrier à partir d’un dossier de preuves. | Affirmations soutenues, URLs exactes, consigne et langue respectées ; aucun envoi implicite. |
| Analyste documentaire | Extraction, comparaison de versions, résumé avec références de pages. | Fichier et version corrects ; perte OCR ou tableau signalée ; aucune confusion entre documents. |
| Assistant d’agenda | Liste, disponibilités, proposition d’événement, application d’un événement autorisé. | Fuseau America/Montreal, changements d’heure, conflits, identité du calendrier et doublons. |
| Assistant CRM | Recherche de contacts, préparation de fiche/note/tâche, écriture autorisée. | Correspondance de personne et organisation ; champs validés ; absence de fusion ambiguë. |
| Assistant de suivi | Rappel et automatisation bornée, état consultable, arrêt et reprise. | Déclenchement et destinataire exacts ; déduplication ; aucune relance après annulation. |
| Programmeur | Code, revue, build et tests lorsque la demande l’exige réellement. | Fichiers et commandes exécutées prouvés ; lecture seule distinguée du progrès effectif. |

Les interfaces d’agenda et CRM sont ici des **propositions**. Le [catalogue Google Calendar/HubSpot de n8n](https://n8n.io/integrations/google-calendar/and/hubspot/) atteste l’existence d’intégrations annoncées, pas leur disponibilité dans Swarmer. Les scopes OAuth, quotas, synchronisation incrémentale et conditions des API correspondantes restent à vérifier avant implémentation.

Pour les écritures, séparer préparation et application rend l’effet visible et testable. Cette séparation ne doit pas créer une nouvelle demande de permission lorsque l’utilisateur a déjà autorisé précisément l’action : la politique canonique et l’autorisation existante déterminent le passage à l’exécution.

## Observabilité et qualité

[OpenInference](https://github.com/Arize-ai/openinference/blob/main/spec/README.md) fournit des conventions de spans pour modèles, agents, outils, récupération et évaluation au-dessus d’OpenTelemetry. La [notice officielle Phoenix du 15 mai 2026](https://arize.com/docs/phoenix/release-notes/05-2026/05-15-2026-otel-semconv-conversion) annonce la conversion des conventions GenAI OpenTelemetry vers OpenInference. C’est une fonctionnalité annoncée par le fournisseur, pas une mesure indépendante.

La page OpenTelemetry interrogée [sur les spans GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/) renvoie une notice de déplacement. La vérification complémentaire du coordinateur a retrouvé le [dépôt officiel des conventions GenAI](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md) : les spans agent/workflow/outils y portent encore le statut **Development**. Épingler la version des conventions choisie avant instrumentation et prévoir la migration des attributs. Cette lecture complémentaire est comptabilisée dans le journal `local-models-sources.json`.

Relier les spans aux vrais identifiants `goal_id`, `task_id`, `node_id`, `job_id`, à la génération du bail et au digest du résultat. Conserver durée de file, découverte, appel, validation, reprise et attente utilisateur séparément. Catégoriser les erreurs : entrée invalide, outil absent, authentification, délai, résultat tronqué, schéma invalide, citation non autorisée, preuve insuffisante, absence de progrès et effet distant incertain.

Ne pas exporter automatiquement prompts, fichiers, courriels, clés, cookies, tokens ou chaînes de raisonnement. Utiliser par défaut des métadonnées et digests ; une capture de contenu explicitement nécessaire doit être filtrée, bornée et soumise à une rétention définie. SQLite et les reçus canoniques restent l’autorité ; une trace absente ou une UI de télémétrie « verte » ne change pas l’état métier.

[Langfuse](https://github.com/langfuse/langfuse) regroupe traces, jeux d’évaluation et gestion de prompts. Le dépôt distingue MIT et dossiers Enterprise `ee`. Il documente aussi une désactivation de télémétrie produit ; cela ne signifie pas que les traces applicatives ne contiendront jamais de données sensibles. Choisir Langfuse ou Phoenix après un petit essai d’ingestion, rétention et masquage, sans installer les deux par défaut.

[promptfoo](https://www.promptfoo.dev/docs/tracing/) permet de tester séquences d’outils, nombre de spans, durées et erreurs. Ajouter quelques assertions déterministes aux tests métier, puis une revue sémantique des réponses. Un juge LLM qui dit « terminé » ne peut pas annuler un échec d’outil ni prouver que le sujet demandé correspond aux sources.

### Cas d’acceptation prioritaires

Ces cas complètent le protocole global rédigé séparément :

1. Demande personnelle sans code : routage correct et aucun appel à un outil de build.
2. Réponse MCP seulement structurée : résultat conservé et validé, sans répétition inutile.
3. Paramètre inconnu ou sortie mal typée : rejet borné, aucun effet ni statut terminé.
4. Annulation pendant un appel : bail invalidé et résultat tardif refusé ; l’effet externe éventuellement déjà commis est identifié.
5. Écriture distante après délai ambigu : recherche par clé d’idempotence, aucune duplication.
6. Recherche de bibliothèques avec sources sur des piscines : rejet sémantique, même si les URLs sont valides.
7. PDF contenant tableau ou scan : provenance de page conservée et erreurs d’extraction visibles.
8. Agenda autour d’un changement d’heure : heure locale, durée et conflit vérifiés.
9. Demande de brouillon : aucun courriel, événement ou changement CRM appliqué implicitement.
10. Plusieurs itérations sans résultat accepté : pause technique honnête ; une lecture seule ne remet pas artificiellement le compteur de progrès à zéro.

## Retours d’expérience et maturité

| Signal primaire | Date/état vérifiables | Conséquence pratique |
|---|---|---|
| [n8n : perte de structuredContent MCP](https://github.com/n8n-io/n8n/issues/27297) | Créé le 19 mars 2026 ; échanges correctifs du 24 au 28 avril ; résultat de recherche mentionnant une livraison en 2.19.0. La récupération longue est partiellement tronquée. | Tester objets, tableaux, content seul, structuredContent seul et les deux ensemble. Ne pas présenter ce défaut historique comme encore universel. |
| [n8n : propagation d’annulation MCP](https://github.com/n8n-io/n8n/issues/28021) | Signalement du 3 avril 2026 ; commentaire de recherche indiquant livraison en 2.17.0. | L’arrêt du parent doit se propager au transport, avec garde contre les résultats tardifs. |
| [PydanticAI : découverte répétée dans Temporal](https://github.com/pydantic/pydantic-ai/issues/4317) | Créé le 13 février et clôturé le 19 février 2026 ; hors fenêtre récente. | Mesurer le nombre de découvertes, préserver le cache et tester son invalidation ; ne pas assimiler cache à autorisation permanente. |

Les commentaires communautaires sont utilisés comme cas de panne et de test. Ils ne constituent ni un classement des frameworks ni la preuve qu’une version actuelle conserve le même problème.

## Licences et points encore à qualifier

| Composant | Ce qui a été vérifié | Décision |
|---|---|---|
| Docling | Code MIT, licences des modèles distinctes. | Vérifier aussi chaque modèle retenu. |
| Langfuse | MIT hors dossiers `ee`. | Choisir les fonctionnalités et artefacts réellement déployés. |
| n8n | Sustainable Use License/Enterprise, source-available, pas licence open source OSI. | Usage interne possible selon les termes ; contrôler les conditions d’un produit embarqué. |
| Autres composants évoqués | Licence exacte de la version ou de l’artefact non vérifiée dans cette collecte. | Lire le fichier de licence de la version épinglée avant adoption. |

La [licence officielle n8n](https://docs.n8n.io/sustainable-use-license/) donne un exemple important : l’usage embarqué des identifiants HubSpot des clients dans une application n’est pas autorisé par la seule Sustainable Use License, contrairement à certains usages internes avec les identifiants de l’entreprise. Il faut donc distinguer automatisation personnelle/interne et intégration commerciale multiutilisateur.

## Complément vérifié : agenda et synchronisation

Le coordinateur a lu trois pages officielles supplémentaires, comptabilisées dans
`local-models-sources.json`. Google Calendar documente la [synchronisation incrémentale](https://developers.google.com/workspace/calendar/api/guides/sync)
par jeton, la pagination et une resynchronisation complète après un jeton invalidé
(HTTP 410). Sa documentation de [création d'événements](https://developers.google.com/workspace/calendar/api/guides/create-events)
explique comment un identifiant choisi par le client aide à éviter les doublons
après un résultat réseau ambigu. Les invitations et notifications doivent rester
des effets explicitement représentés dans le contrat.

Sur iPhone, [EventKit distingue plusieurs niveaux d'accès](https://developer.apple.com/documentation/eventkit/accessing-calendar-using-eventkit-and-eventkitui).
Présenter l'éditeur système ne donne pas automatiquement à l'app la capacité de
relire l'événement effectivement enregistré. Le reçu doit donc préciser si
l'événement a été relu, si une API a confirmé l'enregistrement, ou si seul le
parcours de l'éditeur est terminé. Une capacité agenda peut être mise en œuvre
sans n8n ; le choix dépendra du calendrier réellement utilisé et connecté.

Restent à mesurer : latence et ressources réelles des extracteurs, coût de découverte des outils, comportement de chaque API distante, rétention des traces, maintenance de versions épinglées et précision des évaluations en français. Aucun chiffre de performance, statut de connexion ou résultat de déploiement nouveau n’est revendiqué ici.
