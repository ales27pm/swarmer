# Catalogue d’activités — vérification sur iPhone du 14 septembre 2026

Cette fiche consigne les observations effectuées sur l’iPhone physique après
l’installation de la nouvelle version TestFlight par l’utilisateur. Le contrôle
porte uniquement sur la consultation du catalogue : navigation, recherche,
filtres, fiches et actualisation. Aucun but, job, envoi de message ou accès natif
soumis à permission n’a été déclenché.

## Installation et connexion

- La lecture `devicectl device info apps` a réussi et confirme
  `org.27pm.mongars`, version **0.1.0**, build **20260914035141**.
- Le tunnel CoreDevice IPv6 était opérationnel pendant cette vérification.
- L’entrée **Swarm → Explorer le catalogue d’agents** a ouvert l’écran
  **Catalogue d’agents** sur l’appareil.

La preuve d’installation est conservée dans
`/private/tmp/swarmer-catalog-updated-app.json`. Les validations de l’archive,
de la distribution et du serveur sont documentées séparément dans
[la fiche de release](activity-catalog-release-2026-09-14.md).

## Observations confirmées

| Contrôle | Résultat observé sur l’iPhone |
|---|---|
| Inventaire initial | **13 domaines · 26 profils**, **79 compétences · 15 outils intégrés · 64 à intégrer**. |
| Recherche `agenda` | **2 profils** : **Assistant d’agenda** et **Organisateur personnel**, tous deux vus. |
| Fiche Assistant d’agenda | La fiche s’ouvre ; exemples de demandes, résultats et entrées attendues sont consultables. |
| Lecture du calendrier | **Accès iPhone intégré** est affiché pour la compétence calendrier. La consultation de la fiche ne demande pas de permission iOS et ne lit pas le calendrier. |
| Organisation de semaine et conflits | Les compétences de préparation de semaine et de conflits d’horaire portent **À intégrer**, avec leurs prérequis visibles. |
| Clavier de recherche | Le bouton de fermeture masque le clavier et permet de poursuivre la consultation. |
| Recherche `agenda` + Outils intégrés | **1 profil** affiché. |
| Actualisation | Le relevé est devenu périmé pendant les contrôles ; l’actualisation a rétabli une disponibilité actuelle. Le seuil de 90 secondes n’a pas été chronométré sur l’appareil. |
| Recherche `agenda` + Disponibles pour un but | **0 profil** après actualisation. Une capacité iPhone ou une intégration prévue n’apparaît pas comme une compétence prête pour un but. |
| Recherche `zzzzzzzzzz`, Tout le catalogue | **0 profil**. |
| Recherche sans accent `reviseur` | **2 profils** ; leurs noms n’ont pas été consignés individuellement pendant ce contrôle. |
| Recherche avec accent `réviseur` | **2 profils**, comme la recherche sans accent ; les noms n’ont pas été vérifiés séparément. |
| Recherche effacée | Le champ vide rétablit **26 profils**. Seul le texte de test a été effacé. |
| Tous les domaines, Outils intégrés | **10 profils**. |
| Tous les domaines, Disponibles pour un but | Après actualisation, **1 profil**, **Développeur de projet**, avec **Construire un projet**. |
| Voir les agents connectés | Le lien ouvre bien le registre **Agents**. Une incohérence d’état y a été reproduite, décrite ci-dessous. |

Ces observations qualifient le rendu et les interactions ci-dessus. Les états
affichés décrivent les possibilités et limites du catalogue ; ils ne prouvent
pas l’exécution d’une compétence ni la permission d’accéder à une donnée iPhone.

## Incohérence du registre reproduite

Le registre affichait **2/2 agents actifs**. Le worker de génération Python,
désactivé côté exploitation, était marqué **EN LIGNE** avec un dernier heartbeat
datant de **2 j**. Le worker de construction de projet présentait, lui, un heartbeat
à l’instant. Une actualisation manuelle a conservé cette contradiction. Le
catalogue appliquait correctement son contrôle de fraîcheur et n’affichait qu’un
profil disponible.

Le correctif projette le statut effectif uniquement dans `GET /agents` et
`GET /agents/{agent_id}`. Une déclaration `online`, `busy` ou `draining` devient
`offline` dans la réponse si le `last_seen_at` canonique n’est plus frais selon
le délai configuré. Les statuts `offline` et `unverified`, les cartes, les
identifiants et les timestamps restent inchangés. Le service d’état conserve
ses valeurs brutes pour ses consommateurs internes ; aucune donnée ni action
d’agent n’est modifiée par ces lectures.

Avant correction, neuf cas HTTP ont reproduit le problème. Après correction,
159 tests ciblés passent, couvrant notamment les deux GET, les dates invalides,
futures ou sans fuseau, la limite exacte du délai, les délais configurés et
l’absence de mutation des agents et des jobs. Ruff, mypy, Bandit et la revue
indépendante passent. Le contrat OpenAPI reste valide et inchangé. La suite
serveur complète termine avec **1 219 tests réussis, 9 ignorés et 2 avertissements**
en 283,35 secondes. Le reçu privé
`/private/tmp/swarmer-agent-registry-full-tests.json` confirme une sortie zéro
et des sources inchangées pendant l’exécution.

Le correctif est commité sous
`f72eb2ff9232a15ed0d2d84b737682055701e902`. Les poussées non forcées vers
GitHub et Vibecode ont terminé et les deux branches `main` distantes ont été
relues à cette révision.

## Déploiement et nouvelle lecture physique

La bascule serveur a terminé à **06:02:18 UTC** sur la release
`f72eb2ff9232a15ed0d2d84b737682055701e902-70cdf318721c`.
Le manifeste figé a pour SHA256
`2bc0b44afaa4acbd944884b872bc2c35715b08f4ac6b9dab7cc64caee37480d1`
et la wheel
`70cdf318721c1f3713853471b5599cdefed05639f6493f3b6387b4f37c091534`.
Git, les 62 sources Python de la wheel, le JSON du catalogue et l’installation
ont été comparés. La préparation sur copie privée a conservé le schéma **24**
et les **57 tables**, y compris `sqlite_sequence` et les appairages.

Les contrôles de santé local et HTTPS répondent correctement en version
**0.14.2**. Le worker de projet a redémarré avec la même identité et les mêmes
sources ; son heartbeat est frais. Le worker historique reste désactivé.
La comparaison après bascule ne relève aucune table protégée modifiée ; la base
vivante n’a été ni restaurée ni réinitialisée. Une sauvegarde cohérente est
conservée dans `backups/20260914T060211Z-api-hotfix-f72eb2ff` côté serveur.
Le reçu privé est
`/private/tmp/swarmer-agent-liveness-hotfix-20260914/backend-kit/release-f72eb2ff9232/cutover-result.json`.

Sur **le même iPhone et le même build TestFlight 20260914035141**, le bouton
**Actualiser les heartbeats** a ensuite produit une lecture authentifiée réelle :

- Le total devient **1/2 agents actif**.
- `ubuntu-python-proposal-worker` est **HORS LIGNE**, avec son heartbeat de **2 j**.
- `ubuntu-project-builder` reste **EN LIGNE**, avec son heartbeat **à l’instant**.

Le nouvel arbre d’accessibilité et une capture confirment ces trois résultats.
Aucune reconstruction ni réinstallation de l’application n’a été nécessaire
pour cette correction serveur.

Une relecture indépendante des reçus confirme la cohérence du commit, du
manifeste et de la release, l’absence de changement des tables protégées et
les contrôles de santé. La preuve du GET authentifié de l’application est la
lecture sur iPhone décrite ci-dessus, distincte des contrôles privés du serveur.

Le catalogue a ensuite été remis sur **Tous les domaines → Tout le catalogue**,
avec une recherche vide, **26 profils affichés** et aucun clavier visible.
Le retour par l’écran Swarm affichait **0 agents en travail** et un objectif vide.
La session de contrôle `swarmer-catalog-qa` a été fermée avec succès.

## Difficultés de l’outil de contrôle

- Le daemon initial n’avait pas la configuration d’équipe nécessaire à la
  signature du runner. Cette difficulté a été résolue avec un état isolé sous
  `/tmp/swarmer-catalog-device-20260914` et
  `AGENT_DEVICE_IOS_TEAM_ID=52T7P32J34`, en réutilisant le cache du runner dont
  l’identifiant commence par `c034`. L’application n’a pas été reconstruite.
- La commande standard de défilement ne faisait pas avancer la fiche sur cet
  écran comportant des zones défilantes imbriquées. Un geste de balayage explicite
  a permis de consulter les détails plus bas ; la cause interne n’a pas été isolée.
- Certaines captures de l’arbre d’accessibilité ont pris environ dix secondes ;
  d’autres environ 0,3 seconde. Ces durées décrivent le runner et ne constituent
  ni une mesure de performance de l’application ni un diagnostic de crash.
- La référence d’accessibilité de la touche d’effacement a produit un `v` à la
  place d’un effacement. Une capture fraîche a permis de cibler la vraie touche,
  puis un nouvel arbre a confirmé le champ vide et les 26 profils. Ce défaut de
  ciblage du clavier n’est pas attribué à la recherche du catalogue.
- Une première tentative sur le lien Agents partiellement hors écran a ramené
  la page en haut. La nouvelle lecture de l’écran puis le tap sur le bouton
  entièrement visible ont ouvert le registre ; aucun succès de navigation n’a
  été déduit du seul retour « Tapped ».

Les journaux du daemon et de la session restent dans l’état isolé, notamment
`sessions/swarmer-catalog-qa/events.ndjson` et
`sessions/swarmer-catalog-qa/runner.log`.

## Preuves privées et limites

Captures conservées hors Git :

- `/private/tmp/swarmer-catalog-agenda-before-scroll.png` ;
- `/private/tmp/swarmer-catalog-agenda-details.png` ;
- `/private/tmp/swarmer-catalog-ready-project.png` ;
- `/private/tmp/swarmer-catalog-stale-agent-before.png` ;
- `/private/tmp/swarmer-catalog-stale-agent-after.png` ;
- `/private/tmp/swarmer-catalog-final.png`.

Aucune capture d’écran ni journal brut d’appareil n’est ajouté au dépôt. Cette
campagne ne reteste pas la génération locale, la création ou l’exécution d’un
CRM, Core ML, MLX, GGUF, la mémoire partagée ou les dialogues de permission
natifs. Les preuves antérieures de ces fonctions restent distinctes de cette
vérification du catalogue.
