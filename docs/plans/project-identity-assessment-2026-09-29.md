# M01 — identité durable des projets de recherche et de rédaction

Évaluation du code local le 29 septembre 2026, pendant le gel des sources serveur. Ce document propose un raccordement ; il ne constitue ni une implémentation, ni une qualification en production. Aucun projet utilisateur, service ou modèle n’a été sollicité. Référence : `consolidated-product-plan-2026-09-28.md`, M01, M05 et M11.

## Constat vérifiable dans le code

L’identité persistante existe déjà : `coding_projects.id`, reliée aux exécutions par `goal_project_links`. Le nom de la table est historique ; son schéma ne contient aucune propriété réservée au code. Une identité peut donc exister sans fichier, sans révision, sans modèle et sans index vectoriel.

Elle est pourtant créée tardivement :

| Parcours | Écriture ou lecture actuelle | Conséquence |
| --- | --- | --- |
| Création d’un but, y compris depuis un chat | `GoalManager.create_goal` crée tâche, but, conversation, messages, reçu idempotent et audit dans une transaction. Aucun lien projet. | Une recherche autonome n’a pas d’identité de projet au départ. |
| Planification et évaluation | `_shared_project_memory` appelle `ensure_project` seulement si le service de contexte durable est attaché. | Avec `project_context_enabled=False`, valeur par défaut locale, la mémoire renvoie `no_linked_project`. Cette valeur par défaut ne prouve pas la configuration déployée. |
| Recherche et rédaction | `_worker_payload` transmet objectif, consignes et dépendances ; la rédaction reçoit les sources de recherche. | Ce passage ne crée pas de projet et n’injecte pas lui-même un contexte de mémoire projet dans le contrat du rédacteur. |
| Code | `GoalProjectService.payload` et `capture_result` appellent `ensure_project`. | Le projet existe dès que ce parcours est emprunté. |
| Reprise d’un but terminal | `GoalConversationService.append` crée un nouveau but dans la même conversation et copie le lien par `INSERT … SELECT`. | Si le parent n’est pas lié, aucune ligne n’est copiée. La reprise reste sans identité. |
| Reprise d’une ancienne proposition Python | `GoalManager.reply_goal` peut amorcer la proposition avant la reprise, uniquement si le but n’a aucun lien. | Ce mécanisme dépend actuellement de l’absence d’identité. |
| Initialisation SQLite | `StateService.initialize` rétablit les conversations/messages initiaux manquants. | Elle ne rétablit pas les liens de projet. |
| Lecture des résultats, du graphe ou de la conversation | Projection des liens existants ; `project_id` peut être nul. | Une lecture ne doit pas devenir une migration implicite. |

`GoalProjectService.inherit_project` n’a pas d’appelant trouvé dans `server/app` ; le chemin de reprise effectif est celui de `GoalConversationService.append`.

## Raccordement minimal recommandé

### 1. Une identité dès la nouvelle création

Ajouter un helper d’identité dans un module sans dépendance à `ExecutionEngine`, au worker ou à un fournisseur d’embeddings. Son opération interne reçoit la connexion SQLite déjà transactionnelle et des identifiants serveur ; elle ne commence ni ne valide elle-même une transaction.

Dans la transaction existante de `GoalManager.create_goal`, après l’insertion du but et après les validations d’entrée, créer exactement une ligne `coding_projects` et un lien `goal_project_links`. Le projet, le but, la conversation et le reçu idempotent sont validés ensemble ou annulés ensemble.

La relecture du reçu idempotent retourne le but d’origine sans créer une nouvelle identité. Un ancien reçu désignant un but non lié reste non lié jusqu’à une reprise explicitement traitée ou une migration opérateur ; rejouer une création ne doit pas réécrire arbitrairement l’histoire.

Les options de contexte, compaction, embeddings et récupération hybride ne contrôlent plus l’existence de l’identité. Elles continuent à contrôler leurs propres opérations. La création d’un projet ne lance aucune requête modèle, aucun calcul vectoriel, aucun job et aucune écriture de fichier.

### 2. Une reprise garde l’identité exacte

Pour un but déjà lié, la transaction de reprise doit recopier l’identité exacte du **but actif** résolu par la conversation. Elle ne doit pas s’appuyer sur le but ancien éventuellement présent dans l’URL. Un conflit entre lien existant du nouveau but et lien du parent doit être explicite, jamais masqué par `OR IGNORE`.

Un nouveau but indépendant créé depuis le même chat reste un nouveau projet. Même objectif, même titre, même modèle, même origine de chat ou proximité vectorielle ne prouvent pas une continuation. La relation d’héritage fiable est `goal_conversation_links.parent_goal_id`, avec appartenance cohérente à la conversation durable.

Pour la reprise d’un ancien but sans lien, la solution la plus petite est un raccordement de sa lignée explicite dans la même transaction d’admission, limité et vérifié selon les conditions ci-dessous. Si la lignée est ambiguë ou trop grande, retourner un conflit descriptif sans nouvelle exécution ; ne pas fusionner deux projets pour obtenir un succès apparent.

### 3. Séparer identité et amorçage des fichiers

Une création universelle de liens casse, à elle seule, la conservation de l’ancienne proposition Python :

- `reply_goal` vérifie aujourd’hui `NOT EXISTS(goal_project_links)` avant l’amorçage ;
- `ensure_project` retourne immédiatement lorsqu’un lien existe ;
- une proposition `goal_code_proposals` peut être produite **après** la création d’un nouveau but, pas seulement dans les anciennes bases.

L’amorçage doit donc être indépendant de la création de l’identité. Lorsqu’une reprise explicite transforme une proposition en projet, conserver son contenu exact dans le projet déjà attribué, seulement si aucune révision de ce projet n’existe et si les liaisons but/nœud/job sont cohérentes. Préserver les identifiants sources, le digest et les contraintes d’unicité du reçu ; ne pas remplacer une révision existante ni inventer une chronologie entre propositions contradictoires. Un nouvel appel doit être idempotent.

Ce raccordement peut réutiliser l’amorçage actuel en l’extrayant dans une fonction transactionnelle. Il ne doit pas appeler `ensure_project` avec une seconde connexion alors que `create_goal` ou `append` détient déjà `BEGIN IMMEDIATE`.

### 4. Ne pas confondre identité et capacité locale

Le mode de continuation `iphone_local` utilise actuellement la présence d’un lien projet comme précondition. Après M01, ce lien existera aussi pour une simple recherche sans fichier. Il faut préserver la frontière fonctionnelle actuelle en contrôlant séparément l’existence d’un artifact/révision approprié et l’absence de travail actif, ou étendre explicitement ce contrat dans un lot distinct.

Le test `test_local_continuation_without_project_rejects_without_mutation` ne doit pas simplement disparaître : son cas négatif devient « identité générale présente, mais aucun projet de fichiers compatible ». Un lien n’est ni une preuve de fichier, ni une autorisation de compilation, ni une preuve de travail terminé.

## Rattachement de l’historique : préconditions

Pas de changement de schéma nécessaire pour les nouvelles créations. Le rattachement des anciennes données est une opération de données séparée ; ne pas lancer un balayage global silencieux au démarrage ou à la lecture M05.

Un outil opérateur limité peut d’abord produire un inventaire sans mutation. Pour chaque racine de continuation explicitement sélectionnée, il doit vérifier :

1. Les buts, liens de conversation et références parent existent ; la lignée est acyclique, bornée et dans une conversation cohérente. Les identifiants viennent de la base, jamais d’un texte du modèle.
2. Aucun but du groupe n’a d’appel modèle, job, requête d’embedding ou droit d’exécution actif susceptible de publier sous l’ancienne identité. Vérifier de nouveau dans la transaction de mutation, avec la barrière d’admission appropriée.
3. Zéro identité existante permet d’en créer une. Une seule identité valide permet de rattacher les membres manquants. Deux identités distinctes interdisent la fusion automatique.
4. Aucune révision, proposition, mémoire ou provenance existante ne contredit le rattachement. Une proposition Python à conserver est amorcée avant que le lien ne rende l’ancien chemin inaccessible. Plusieurs propositions divergentes sans ordre de révision fiable sont signalées, pas remplacées.
5. Aucun lien existant n’est déplacé. Les sources, épisodes, messages, reçus et versions ne sont ni réécrits ni supprimés. Les projections et vecteurs ne sont pas assimilés à leurs sources.
6. Le changement est transactionnel, audité, idempotent et limité par nombre de lignées/buts. Les conflits sont comptés avec une raison ; aucun résultat partiel par lignée n’est validé.
7. Un export de sauvegarde et un inventaire avant/après précèdent tout usage sur données réelles. Une restauration d’ancienne base ne doit pas écraser des écritures plus récentes.

Cas particulier : le démarrage crée une conversation isolée pour un ancien but sans lien de conversation. Cette opération ne prouve pas son appartenance à un autre projet ; il conserve une identité indépendante, même si son objectif est identique.

### Frontière à durcir avant une migration permissive

`ProjectMemoryService._read_sources`, `_assert_response_current`, `ProjectContextService._sources_locked` et `source` retrouvent certains messages par leur `conversation_id`, puis vérifient le projet du but qui référence cette conversation. Ils supposent donc que la conversation a une identité de projet homogène ; ils ne vérifient pas tous le projet propre à `goal_messages.goal_run_id`.

Une lignée ancienne contenant déjà plusieurs projets ne doit surtout pas être « réparée » en liant simplement ses membres restants. Avant d’autoriser de tels historiques à la récupération, vérifier aussi la portée de chaque source par son but d’origine **avant les limites SQL**, et rejeter/exclure les sources non liées ou contradictoires. Les snapshots historiques non vérifiables restent des preuves de ce qui avait été enregistré, pas une autorisation de réinjecter le texte.

Le constat initial découlait de la lecture des requêtes. La reproduction isolée puis le correctif local sont consignés ci-dessous ; aucun de ces essais n’a interrogé la production.

## Ce que M01 permettra, et ce qu’il ne prouve pas

Avec une identité présente avant le premier appel, le planificateur et l’évaluateur pourront obtenir une récupération projet traçable ; les épisodes de la même lignée seront accessibles selon les limites et filtres M11. Les messages et plans existants deviendront éligibles aux projections actuelles, avec leur rôle et leur provenance.

Cela ne prouve pas que la recherche sera sémantique : le reçu peut encore indiquer un mode lexical ou un repli. Cela ne rend pas non plus tout contenu utile ni vérifié. La sélection de faits durables, la mémoire documentaire et les contrats d’injection propres au rédacteur et au chercheur restent des travaux distincts. Le prompt de rédaction actuel reçoit les consignes et les sources de dépendances, mais aucun champ mémoire projet explicite ; ne pas annoncer M01 complet pour chaque rôle sur la seule création de liens.

Les tâches directes `POST /tasks` et les échanges `POST /chat` ne constituent pas automatiquement des projets autonomes. Toute extension de leur contrat doit disposer d’un rattachement explicite vers un projet existant ou d’une conversion en nouveau but ; ne pas utiliser un texte de chemin ou une URL comme identité.

## Cas de validation à ajouter après le gel

| Groupe | Cas et résultat attendu |
| --- | --- |
| Création générique | But recherche/rédaction créé avec tous les flags désactivés et sans passerelle de code : une identité non nulle, aucun fichier, job ou appel modèle. |
| Atomicité | Conversation chat inexistante ou erreur après insertion : aucune tâche, identité orpheline, conversation ou reçu résiduel. |
| Idempotence | Deux créations concurrentes avec même acteur/clé donnent le même but et un seul projet ; rejouer après redémarrage conserve l’identité. |
| Séparation | Même objectif, chat et titre mais clés différentes : projets différents ; récupération du projet A ne retourne aucune source ou épisode de B. |
| Reprise | But recherche terminé puis réponse, nouvelle réponse via ancien identifiant, concurrence et redémarrage : une lignée et une identité, historique conservé. |
| Ancien historique | Lignée non liée cohérente : rattachement unique ; une identité déjà présente : conservation ; deux identités ou parent incohérent : aucune mutation. |
| Fichiers anciens | Proposition produite avant ou après création du lien : reprise conserve exactement le fichier ; révision existante préservée ; double amorçage ne crée pas de doublon. |
| Local iPhone | Recherche avec identité mais sans artifact : continuation de fichiers refusée sans mutation ; projet compatible : comportement existant conservé ; travail actif reste bloquant. |
| Lecture passive | Graph, conversation, M05 et relecture d’un ancien reçu ne créent ni lien, ni vectorisation, ni requête modèle. |
| Source exacte | Conversation synthétique liée à A et B : messages de B exclus de A, y compris validation du reçu et consultation de source, avant les plafonds ; source ambiguë exclue. |
| Appels et preuves | Planificateur puis évaluateur d’un but de recherche partagent le projet ; reçu et identifiants exacts présents ; mode déclaré correspond au chemin lexical/sémantique réellement exécuté. |
| Migration concurrente | Un job démarre ou un lien change entre inventaire et application : nouvelle vérification refuse le lot concerné ; aucune identité partiellement installée. |

## Fichiers envisagés et séquençage

1. Nouveau helper d’identité, `goal_manager.py:create_goal`, `goal_conversation.py:append` et tests ciblés création/reprise : invariants pour nouvelles données.
2. `goal_project.py` et préambule de `reply_goal` : amorçage indépendant et conservation de fichiers, avec contrôle de l’identité du but actif.
3. Contrôle `iphone_local` et ses tests : préserver la distinction identité/artifact.
4. Reproduction puis correction bornée des lecteurs `project_memory.py` et `project_context.py` si le cas de conversation conflictuelle confirme l’écart de portée.
5. Outil d’inventaire/migration explicite, séparé du démarrage et des lectures ; essais sur copies synthétiques puis qualification avant toute donnée réelle.
6. M05 expose les identités et reçus effectivement enregistrés ; l’injection mémoire propre à chaque rôle fait l’objet d’une validation de contrat distincte.

Aucune de ces modifications sources/tests n’a été effectuée dans cette évaluation.

## Complément : frontière de provenance reproduite puis corrigée localement

À la demande de la revue, une fixture temporaire hors dépôt a reproduit cette séquence, uniquement avec les services normaux : créer A, l’annuler avant démarrage, répondre pour créer B dans la même conversation encore non liée, préparer le contexte de A puis celui de B. Les deux appels à `ensure_project` sont ceux de `POST /goals/{id}/context` lorsque cette option est activée. Résultat : une conversation avec deux identités distinctes, zéro job/appel modèle, et `PRAGMA foreign_key_check` sans anomalie. Cela établit la possibilité par les services, sans affirmer sa présence en production ni un accès entre utilisateurs.

Le script `/tmp/test_m01_conversation_provenance.py` a donné **1 contrôle de topologie réussi et 5 régressions rouges** : exigences du contexte, consultation directe d’une source, récupération lexicale, conversation récente du reçu et revalidation d’un reçu après changement de portée de sa source. Le dernier cas simule explicitement une modification de rattachement dans sa base temporaire ; ce changement n’est pas présenté comme une route utilisateur.

La revue indépendante a aussi reproduit **6 courses rouges** dans le parcours worker : déplacement du propriétaire, suppression ou correction d’un message pendant un fournisseur synthétique, avec réponse normale ou erreur du fournisseur. L’ancien extrait ressortait du tableau de candidats et pouvait recevoir un vecteur après sa modification.

Après levée du gel serveur, le correctif a été limité à `project_memory.py`, `project_context.py` et un fichier de régressions dédié. Il applique la portée du but source et son appartenance exacte à la conversation avant les plafonds SQL ; revalide les sources dans la transaction de projection, avant le fournisseur, dans la transaction de persistance vectorielle et dans le snapshot final ; contrôle aussi les reçus déjà enregistrés. Une continuation dont les deux buts appartiennent au même projet garde ses sources. Sans aucune identité, une continuation explicitement reliée conserve les messages de ses membres également non liés ; une source liée à un projet connu est exclue de cette portée non liée.

Cette validation est un snapshot, pas un verrou conservé après le retour ni une possibilité de rappeler les données déjà envoyées avant une modification concurrente. Les contrôles d’admission des consommateurs restent nécessaires. La conversation brute des payloads est traitée séparément dans `GoalManager.recent_conversation` par l’autre lot de revue ; l’historique affiché à l’utilisateur reste intact.

L’identité M01 universelle et la migration décrites plus haut ne sont toujours pas implémentées par ce correctif. La qualification des sources et les reçus de tests courants sont suivis dans le rapport d’implémentation général.

## Raccordement M01 implémenté localement

Cette section remplace le statut « non implémenté » de l’évaluation initiale ci-dessus pour la couche d’identité uniquement. Les sources locales créent maintenant l’identité durable dans la transaction de `GoalManager.create_goal`, sans dépendre du moteur de code ni des options mémoire. Un replay idempotent ancien retourne le reçu existant sans migration.

Une réponse explicite résout le but actif de la conversation, puis conserve son projet. Le helper `project_identity.py` vérifie une chaîne complète, acyclique, de 128 buts au maximum. Une lignée ancienne sans identité peut être rattachée dans cette écriture ; une identité unique déjà présente est conservée. Des identités distinctes, une provenance enregistrée contradictoire, une lignée incomplète ou trop grande produisent un conflit transactionnel, sans nouvelle exécution ni message faussement accepté. Aucun lien connu n’est déplacé.

Le rattachement contrôle les appels modèle, embeddings, compactions, jobs, outils et requêtes de capacité actifs. Lorsqu’une identité existante est enrichie de membres anciens, le contrôle inclut aussi les autres buts déjà reliés à ce projet dans la limite admise. Des enregistrements `started` conservés sont bloquants même s’ils paraissent anciens : ce parcours ne les annule pas et ne les déclare pas expirés.

L’amorçage d’une proposition Python est indépendant de l’identité et se fait seulement dans une réponse explicitement admise. Il fonctionne aussi lorsque la proposition attend une décision sans que le but soit terminal. Le contenu, le hash, le job, le nœud, la tâche source et le chemin sont vérifiés ensemble. Une révision existante reste inchangée ; plusieurs propositions aux contenus divergents sans version fiable ne sont pas arbitrairement fusionnées. Le snapshot conservé demeure un brouillon privé et n’écrit aucun fichier sur disque. Si un consommateur actif empêche cet amorçage, toute la réponse est annulée transactionnellement avec un conflit explicite.

`GoalProjectService.ensure_project` est désormais une vérification passive. Les anciennes données non liées restent consultables telles quelles, mais une opération nécessitant une identité renvoie une demande de reprise explicite. Aucune migration globale au démarrage ou à la lecture n’a été ajoutée. Une identité générique ne permet pas une continuation `iphone_local` : ce parcours exige une révision de fichiers cohérente et aucun travail actif. Elle n’accorde aucun nouveau droit d’exécution ou de compilation.

Les fixtures d’isolation reproduisent explicitement les anciens historiques divisés/non liés ; elles ne reposent plus sur un défaut de création actuel. Les tests conservent les assertions de portée, de non-mutation des lectures, de conservation des révisions et de redémarrage. Les essais utilisent uniquement SQLite temporaire et des reçus synthétiques ; aucun projet utilisateur, modèle réel, appareil, SSH ou déploiement n’a été sollicité. M01 ne constitue toujours pas une preuve d’injection mémoire dans tous les rôles, ni de récupération sémantique ou de qualification iPhone.

Reçus locaux M01 : les premières régressions ont donné 8 échecs attendus ; les cas supplémentaires « proposition prête non terminale » et « outil encore actif » ont aussi été reproduits avant correction. Un groupe de 231 tests des surfaces affectées a ensuite réussi. La revue indépendante a détecté un dépassement d’une unité à 128 buts : la reprise terminale pouvait créer un 129e but. Cette limite est maintenant contrôlée avant l’ajout ; un message non terminal à 128 reste accepté. Les 49 tests de création, conversation, proposition et continuation locale ont repassé après ce dernier changement (`/tmp/swarmer-m01-review-final.log`, 15,42 s), ainsi que Ruff, le formatage et mypy sur les modules M01. Les deux avertissements sont les dépréciations existantes Starlette/httpx et AnyIO. La suite serveur intégrale et la qualification déployée relèvent des étapes suivantes ; ces reçus ne les remplacent pas.
