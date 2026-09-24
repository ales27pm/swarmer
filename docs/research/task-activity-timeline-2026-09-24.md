# Chronologie des opérations dans l’app iOS

Date : 24 septembre 2026. Référence inspectée : `59891ad`.

**État : audit statique / proposition non implémentée.** Ce document décrit le code local, sans preuve du comportement du serveur déployé ni de l’iPhone. L’audit n’a exécuté aucun modèle, test, déploiement ou modification de données. Aucun `AGENTS.md` applicable n’a été trouvé dans le dépôt ni ses répertoires parents. Les changements préexistants ont été préservés.

L’app peut déjà présenter une première chronologie à partir des preuves conservées. Le détail en direct à l’intérieur d’une itération worker exige une instrumentation supplémentaire. L’objectif est d’expliquer les opérations observables, sans afficher ni reconstruire les pensées internes des modèles.

## État actuel

Trois catégories sont à distinguer :

- **Snapshots** : dernier état d’une tâche, d’un nœud, d’un outil ou d’une révision; ils ne racontent pas chaque transition.
- **Événements persistés** : faits enregistrés lors de certaines transitions, avec identité et date; ils permettent un historique partiel.
- **Détails internes manquants** : opérations du worker non transmises au serveur pendant l’exécution; ils ne peuvent pas être déduits honnêtement d’un statut « en cours ».

| Besoin | Preuves présentes | Exposition et limite actuelle |
| --- | --- | --- |
| Agent, affectation, attente, fin | `agent_jobs` conserve agent, statut, tentative, prise, heartbeat et fin. L’audit enregistre mise en file, prise, fin, expiration, reprise et quarantaine. | L’iOS présente surtout des snapshots de nœuds. Un heartbeat prouve une présence, pas une progression. Voir [schéma des jobs](../../server/app/services/state_service.py#L357), [prise du travail](../../server/app/services/agent_dispatcher.py#L591) et [expiration du lease](../../server/app/services/agent_lease_reaper.py#L253). |
| Modèle et durée du contrôle serveur | `goal_model_calls` conserve rôle, fournisseur, modèle, statut, latence, erreur, dates et lien au but. | Pas de projection mobile de ces appels. Cette table ne représente pas tous les appels des workers : certains sont seulement budgétés par `goal.codegen.reserved` ou `goal.writing.reserved`. Voir [schéma](../../server/app/services/state_service.py#L745), [réservation](../../server/app/services/goal_manager.py#L1601), [fin](../../server/app/services/goal_manager.py#L1677) et [budget worker](../../server/app/services/goal_manager.py#L2301). |
| Outils directs | `tool_calls`, événements de proposition et de fin, arguments/résultats publics expurgés. | L’écran montre nom, statut, arguments, résultat et erreur; pas une séquence complète début/progression/fin. La différence entre création et mise à jour peut inclure l’attente d’approbation et ne vaut pas une durée d’exécution. Voir [projection publique](../../server/app/services/approval_binding.py#L288) et [affichage des outils](../../mobile/app/task/[id].tsx#L149). |
| Fichiers du projet | Révisions avec snapshots, identifiants du worker et hash. | Le contenu d’une révision est consultable après réception du résultat. Une révision générée, un fichier écrit dans le bac à sable de vérification et une révision appliquée au workspace sont des faits distincts. Voir [persistance](../../server/app/services/goal_project.py#L308) et [revue mobile](../../mobile/src/components/goal-project-review.tsx#L88). |
| Lectures attestées | Le worker distingue les fichiers présents dans le prompt final; il collecte des reçus de lecture des instructions avec hash. Le serveur renvoie `guidance_reads` dans la revue. | Le parseur mobile ne conserve pas `guidance_reads`. `focus_paths` représente une demande de lecture, pas sa réalisation. Voir [lectures worker](../../workers/project-worker/project_worker.py#L1872), [projection serveur](../../server/app/services/goal_project.py#L407) et [parseur mobile](../../mobile/src/lib/api/project.ts#L184). |
| Commandes et vérifications | Reçus avec commande, sortie bornée, code de sortie et durée mesurée. | Consultables après l’itération, sans événement indiquant quelle commande tourne actuellement. Voir [production des reçus](../../workers/project-worker/runtime.py#L434) et [affichage dépliable existant](../../mobile/src/components/goal-project-review.tsx#L17). |
| Progression du modèle worker | Le transport Ollama mesure chunks, premier contenu, durée et compteurs. | Ces métriques restent en mémoire; celles du transport sont journalisées sur timeout, mais elles ne sont pas envoyées dans le résultat. Voir [mesures](../../workers/project-worker/project_worker.py#L1661), [fin de transport](../../workers/project-worker/project_worker.py#L1753) et [soumission du résultat](../../workers/project-worker/project_worker.py#L2105). |
| Attentes et erreurs | Phases du but, statuts des nœuds, autorisations, erreurs et résultats existent. | L’app sait expliquer certaines attentes, mais leur début, leur fin et leur historique ne sont pas unifiés. Voir [phases et libellés](../../mobile/src/screens/goal-detail-content.tsx#L42). |

Le protocole worker transporte actuellement prise du travail, heartbeat, résultat final et demandes de capacités; il ne possède pas de contrat général de progression par opération. Sources : [modèles des requêtes](../../server/app/models.py#L165), [client worker](../../workers/file-worker/file_worker.py#L275), [routes serveur](../../server/app/main.py#L2200).

## Audit, transport et reconnexion

Le [journal d’audit](../../server/app/services/audit_log.py#L38) est durable, ordonné par identifiant SQLite et chaîné par hash. Il contient `task_id`, `trace_id`, acteur, date et payload. Les traces utilisent tantôt la tâche enfant, tantôt le but : un filtre sur un seul `trace_id` manquerait une partie des opérations.

La [route `/audit`](../../server/app/main.py#L2440) accepte `after_id` pour une lecture ascendante, mais reste globale, sans filtre tâche/but. Le [client mobile](../../mobile/src/lib/api/client.ts#L1407) n’expose pas ce curseur. Les Réglages demandent les 15 derniers événements et affichent seulement type, hash et date : [interface](../../mobile/app/(main)/settings.tsx#L169), [chargement](../../mobile/app/(main)/settings.tsx#L219). Les payloads d’audit ne doivent pas devenir automatiquement les payloads d’une nouvelle interface publique : leur projection actuelle n’est pas le contrat strict d’une chronologie.

Le [WebSocket](../../server/app/services/websocket_notifications.py#L20) distribue principalement des invalidations expurgées, avec livraison potentiellement répétée. Son enveloppe mobile est `{type,payload}`, sans séquence d’historique. La [projection de confidentialité](../../server/app/services/event_privacy.py#L328) enlève les contenus sensibles du flux partagé. La [reconnexion mobile](../../mobile/src/lib/sync/live-sync-provider.tsx#L117) recharge un bootstrap, donc des snapshots.

La [réplique SQLite](../../mobile/src/lib/state/replica.ts#L44) ne possède pas de table d’activité. Sa [table de routage](../../mobile/src/lib/state/replica.ts#L124) ne conserve pas `agent.job.*`. La [projection des travaux d’une tâche](../../server/app/services/task_execution.py#L12) est limitée à 20 nœuds et ne fournit pas leur historique complet.

## Vue proposée pour l’utilisateur

Une section **Activité** commune aux écrans de tâche et de but présente d’abord une synthèse : opération courante, agent, modèle connu, temps écoulé depuis un début attesté et fraîcheur de la connexion. Plusieurs opérations simultanées restent visibles comme telles. Une attente d’autorisation, un worker indisponible ou une absence de mise à jour portent des libellés distincts.

La chronologie affiche des lignes courtes avec icône, action, état et durée disponible. Les exemples « Lecture de `app.py` », « Vérification `python -m pytest` » ou « Révision prête à relire » ne sont affichés qu’après réception de l’événement correspondant. Le texte produit par un modèle ne constitue pas une preuve d’exécution.

Chaque ligne peut se déplier pour montrer, selon les droits et les données disponibles :

- agent, modèle, tentative et identifiants de corrélation;
- heures de début/fin, durée mesurée et provenance de la preuve;
- chemins relatifs lus ou modifiés, hash ou référence de révision;
- commande autorisée, code de sortie et résultat expurgé;
- erreur, raison d’attente, reprise ou interruption;
- lien vers les détails privés consultables sur demande.

Les données historiques incomplètes portent « durée indisponible » ou « détail non enregistré ». Une coupure affiche « hors ligne — dernière mise à jour… »; le temps qui passe ne fait pas avancer artificiellement une opération. L’utilisateur peut consulter les événements précédents sans perdre sa position à chaque arrivée.

## Trois étapes prioritaires

### 1. Contrat durable et API de lecture par tâche/but

Ajouter `server/app/services/activity_contracts.py` et `activity_service.py`, une table/migration dans [state_service.py](../../server/app/services/state_service.py), puis les routes dans [main.py](../../server/app/main.py). Utiliser les preuves existantes pour l’historique disponible. Enregistrer les nouvelles transitions dans la même transaction que le changement d’état; préserver la provenance des événements historiques importés.

Contrat proposé : `schema_version`, `event_id`, `sequence`, `goal_run_id`, `task_id`, `node_id`, `job_id`, `attempt`, `operation_id`, `parent_operation_id`, `kind`, `phase`, `actor`, `model_id`, `occurred_at`, `recorded_at`, `duration_ms`, `summary`, `detail_ref`, `evidence_source`. Les champs absents restent nuls; aucun ancien début d’exécution n’est inventé à partir d’une création.

Prévoir une lecture authentifiée `/tasks/{id}/activity` et `/goals/{id}/activity`, paginée par curseur opaque lié au périmètre et à l’incarnation du stockage. Réponse : `events`, `next_cursor`, `has_more`, borne haute de lecture. Un identifiant de séquence définit l’ordre de réception serveur, sans prétendre établir un ordre causal entre workers concurrents. Les liens parent/opération portent cette causalité lorsqu’elle est connue.

Critères d’acceptation :

- Une tâche racine retrouve ses opérations et celles de ses enfants sans mélanger deux buts; les continuations sont explicitement reliées.
- La pagination complète ne perd ni ne duplique un événement, y compris pendant des écritures concurrentes; la remise à zéro du stockage invalide proprement l’ancien curseur.
- Chaque événement possède une identité stable et un schéma validé; les transitions d’état nouvelles et leur événement sont atomiques.
- Les sources existantes sont projetées avec une liste explicite de champs autorisés; prompts, credentials, leases et sorties privées ne traversent pas cette API de synthèse.
- Une lecture d’activité ne lance ni réconciliation métier, ni modèle, ni travail supplémentaire. Les durées inconnues restent inconnues.

### 2. Événements des opérations worker réellement exécutées

Ajouter un endpoint authentifié d’événements dans [models.py](../../server/app/models.py), [main.py](../../server/app/main.py) et [agent_dispatcher.py](../../server/app/services/agent_dispatcher.py). Étendre le [client de protocole](../../workers/file-worker/file_worker.py), puis instrumenter [project_worker.py](../../workers/project-worker/project_worker.py) et [runtime.py](../../workers/project-worker/runtime.py).

Émettre les débuts/fins de modèle, lectures confirmées, changements acceptés dans une révision, commandes de vérification, attentes explicites et erreurs. L’événement décrit le fait observé par le worker; il ne promet pas une validation indépendante de toutes ses déclarations. Associer chaque opération à une tentative et à la révision concernée. Conserver le modèle effectivement utilisé pour cet appel, plutôt que déduire son identité de la fiche courante de l’agent.

Critères d’acceptation :

- Les événements sont liés à l’agent authentifié et au lease courant; un worker ne peut pas choisir arbitrairement la tâche ou l’agent affiché.
- Déduplication par `(job_id, lease_generation, producer_event_id)`; une retransmission identique ne crée pas de doublon et une charge différente sous la même clé est refusée.
- Lease périmé, tentative remplacée et résultat déjà terminal sont traités explicitement. Une reprise ouvre une nouvelle tentative et ne réactive pas l’ancienne.
- Les durées proviennent d’une horloge monotone du producteur; heure déclarée et heure de réception serveur restent distinctes.
- Un échec de livraison de télémétrie ne rejoue jamais une commande métier. Les limites, la reprise de livraison et les éventuelles lacunes de télémétrie sont testées et visibles.
- La réception de chunks modèle peut alimenter des compteurs bornés, jamais des pensées internes. Un heartbeat seul n’est pas affiché comme une lecture, une modification ou une progression.

### 3. Chronologie iOS, cache et rattrapage

Ajouter un composant d’activité partagé entre [écran de tâche](../../mobile/app/task/[id].tsx) et [contenu du but](../../mobile/src/screens/goal-detail-content.tsx), des types/parseurs dans `mobile/src/lib/api`, puis la table et le curseur dans [replica.ts](../../mobile/src/lib/state/replica.ts). Étendre [live-sync-provider.tsx](../../mobile/src/lib/sync/live-sync-provider.tsx) pour invalider et rattraper l’activité. Le WebSocket peut rester une notification de borne haute; les détails arrivent par l’API authentifiée.

Critères d’acceptation :

- La synthèse et les lignes dépliables couvrent agent, modèle connu, outils, fichiers, commandes, résultat, durée et attente/erreur lorsque les données existent.
- Les opérations concurrentes sont regroupées sans être artificiellement sérialisées; proposition, exécution, révision et application restent distinctes.
- Doublons, arrivées retardées, reconnexion, retour au premier plan et pagination produisent la même chronologie que l’API après rattrapage.
- Un changement de serveur ou de jumelage isole le cache et invalide les réponses en vol; aucune activité privée d’une connexion précédente ne réapparaît.
- Le cache hors ligne conserve les événements déjà obtenus et leur fraîcheur; il ne transforme pas une opération sans fin reçue en succès.
- Tests d’accessibilité, longues listes, expansion des détails et maintien de position; validation sur iPhone requise avant d’annoncer le résultat mobile comme vérifié.

## Vérification à prévoir

Réutiliser les invariants des tests existants : [projection des tâches](../../server/tests/test_task_execution.py), [confidentialité des événements](../../server/tests/test_event_privacy.py), [notifications WebSocket](../../server/tests/test_websocket_notifications.py), [synchronisation mobile](../../mobile/src/lib/sync/live-sync.test.ts), [réconciliation mobile](../../mobile/src/lib/sync/live-sync-provider.test.tsx) et [réplique](../../mobile/src/lib/state/replica.test.ts). Ajouter les contrats, autorisations, transactions, curseurs, déduplication et scénarios de reprise spécifiques à cette chronologie.

L’inférence locale sur iPhone possède son propre état dans [local-inference.ts](../../mobile/src/lib/local-inference.ts). Son rattachement éventuel à cette chronologie devra garder une provenance « iPhone », une identité de génération et les mêmes garanties de confidentialité; il ne faut pas assimiler une génération locale à une exécution serveur.

Ce document est une proposition de travail. Aucun des nouveaux fichiers, endpoints, événements ou composants proposés n’a été implémenté dans le cadre de cet audit.
