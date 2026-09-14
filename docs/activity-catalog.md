# Catalogue des activités

La source canonique est [`server/app/data/activity_catalog.json`](../server/app/data/activity_catalog.json), au format `schema_version: "1.0"`. Elle décrit 13 domaines, 26 rôles et 79 compétences pour explorer des usages professionnels et personnels. Ce périmètre est extensible ; il ne prétend pas couvrir toutes les activités.

Un **domaine** regroupe des usages. Un **rôle** décrit une mission et référence des compétences. Une **compétence** précise ses entrées, son livrable et ses prérequis. Un rôle peut mêler des opérations prises en charge et des opérations prévues ; sa présence dans le catalogue ne crée pas un worker et ne signifie pas que toute sa mission est réalisable.

| Domaine | Rôles |
|---|---|
| Développement logiciel | Développeur de projet ; Réviseur de code |
| Infrastructure et exploitation | Diagnosticien système ; Responsable de livraison |
| Recherche et connaissances | Documentaliste ; Analyste de veille |
| Fichiers et données | Explorateur de fichiers ; Analyste de données |
| Rédaction et documents | Rédacteur ; Traducteur-réviseur |
| Design et création | Concepteur d’interfaces ; Créateur de supports visuels |
| Projets et coordination | Planificateur de projet ; Coordinateur de projet |
| Clients et ventes | Gestionnaire CRM ; Assistant aux soumissions |
| Communications et réunions | Assistant de correspondance ; Secrétaire de réunion |
| Agenda et quotidien | Assistant d’agenda ; Organisateur personnel |
| Budget et administration | Analyste de budget ; Assistant administratif |
| Apprentissage et habitudes | Tuteur ; Accompagnateur d’habitudes |
| Déplacements et logistique | Planificateur de déplacements ; Coordinateur d’achats |

## Correspondance avec l’exécution

`execution.kind` distingue trois catégories :

- **`worker` — 9 compétences prises en charge dans le code.** `target` est exactement un identifiant de `SUPPORTED_AGENT_SKILLS` dans [`agent_card.py`](../server/app/services/agent_card.py) : `workspace.list_dir`, `workspace.read_text`, `research.query`, `code_review.git_status`, `code_review.git_diff`, `code_review.git_show`, `code_review.static_analysis`, `code.generate_python` ou `code.build_project`.
- **`iphone` — 6 ponts natifs pris en charge dans le code.** `target` est exactement un nom déclaré dans [`models.py`](../server/app/models.py) : `iphone.location.current`, `iphone.contacts.lookup`, `iphone.calendar.events`, `iphone.photos.pick`, `iphone.mail.compose` ou `iphone.sms.compose`. Ce sont des demandes corrélées à une tâche et soumises au parcours d’approbation de l’appareil, pas des skills worker supplémentaires.
- **`planned` — 64 compétences descriptives.** `target` vaut toujours `null`. Leur adaptateur métier, leurs entrées/sorties et leur intégration restent à développer et à valider. La description d’un usage ne suffit pas à lui attribuer une cible existante.

Pour les deux premières catégories, « pris en charge dans le code » ne signifie ni connecté, ni autorisé, ni disponible maintenant. L’état effectif dépend du registre, de la fraîcheur des workers, de la politique, des connexions configurées et de l’appareil. Par exemple, `research.query` exige un adaptateur de recherche configuré ; les composeurs iPhone ne garantissent pas l’envoi ou la livraison d’un message. Ce fichier ne déclare aucune application tierce connectée et ne constitue pas une preuve de validation sur appareil.

Les fonctions de planification, d’évaluation et de mémoire du contrôle central ne sont pas converties en workers de gestion de projet. Les compétences métier correspondantes restent `planned`. Le fichier historique `configs/mobile-capabilities.yaml` n’est pas utilisé comme autorité pour les cibles de ce catalogue.

## Composition des activités

Les champs `inputs`, `output` et `requirements` décrivent les dépendances utiles à une future composition : sources → synthèse ; spécification → projet → revue ; notes → compte rendu → actions proposées. Ces descriptions ne sont pas un DAG exécutable. Un plan réel reste limité aux compétences annoncées par les agents admissibles et doit passer les validateurs et contrôles transactionnels existants.

Les exemples sont des suggestions de demandes, sans lancement automatique. Une proposition de livraison, de soumission, d’achat ou d’horaire ne constitue pas l’exécution de cette action. Les compétences conservent explicitement ces limites dans leurs livrables et prérequis.

## Consultation dans l’application

Ouvrir **Swarm → Explorer le catalogue d’agents**. La recherche porte sur les profils, leurs exemples et leurs compétences ; les accents ne changent pas les résultats. Les filtres permettent de choisir un domaine, les outils intégrés ou les compétences disponibles pour démarrer un but. Chaque fiche développe les entrées attendues, le résultat et les prérequis. Le lien « Voir les agents connectés » ouvre le registre effectif.

L’application lit `GET /catalog/activities`, authentifié par un appareil appairé. La réponse ajoute `generated_at` et une disponibilité à chaque compétence, avec `Cache-Control: no-store`. Cette route ne modifie ni la base, ni les politiques, ni les agents, et ne lance aucune tâche.

| État | Interprétation |
|---|---|
| `goal_ready` | Au moins un agent admissible et récent annonce la compétence ; le démarrage du but revérifie sa disponibilité. |
| `parameters_required` | Un agent admissible existe, mais des paramètres explicites sont nécessaires pour cette opération. |
| `worker_unavailable` | Aucun agent admissible et récent n’annonce actuellement la compétence. |
| `iphone_request` | Le pont natif existe ; une demande corrélée et les autorisations de l’appareil restent nécessaires. |
| `policy_denied` | Les réglages désactivent cette opération. |
| `unknown` | La disponibilité ne peut pas être établie. |
| `planned` | L’intégration n’existe pas encore. |

L’app neutralise les indications de disponibilité si une actualisation échoue, si le relevé ou sa réception date d’au moins 90 secondes, ou si l’horodatage est trop éloigné dans le futur. Les fiches déjà reçues restent lisibles. Les identifiants d’agents sont bornés à 250 par compétence ; ce résumé ne remplace pas le registre complet ni le contrôle au moment de l’exécution.

## Évolution du fichier

Conserver des identifiants ASCII stables en minuscules et uniques dans chaque tableau. Chaque rôle référence un domaine existant et des compétences existantes, sans doublon. Chaque compétence doit être utilisée par au moins un rôle. Les cibles exécutables gardent le même identifiant que la compétence ; toute nouvelle cible nécessite une implémentation et son inscription explicite dans les contrats du runtime avant de quitter `planned`.

Le JSON réside dans les données du paquet `app`. Toute modification du catalogue doit être vérifiée avec son chargeur, les tests de contrat et le contenu du wheel produit ; une modification de texte ne doit pas élargir l’allowlist d’exécution.
