# API de domaine de l’application

Le contrat des fonctionnalités de l’app se trouve dans
`mobile/src/lib/application-api/`. L’interface appelle ce contrat en mémoire.
Le transport HTTPS iOS et les tests appellent les mêmes handlers. Ubuntu reste
autoritaire pour les buts, tâches, projets, autorisations et mémoire serveur ;
la base SQLite du téléphone reste une réplique, jamais une autorisation.

## Découvrir les commandes

Le catalogue comprend actuellement 67 commandes et est produit par le registre exécutable, avec version, schéma
d’entrée, provenance, effet, conditions de disponibilité et interaction iOS.
Les commandes actuelles couvrent la connexion, la synchronisation, les buts et
tâches, les conversations, les approbations, la mémoire, les agents, les modèles,
la génération locale, les revues de projets et les capacités iPhone. La présence
d’un handler ne prouve pas la disponibilité de son serveur, modèle ou permission :
ses prérequis restent vérifiés au moment de l’appel.
Le catalogue des activités métier est une autre ressource : une compétence
« à intégrer » n’est pas une commande exécutable.

Les façades `server.ts`, `local-inference.ts` et `local-settings.ts` conservent
les signatures utilisées par les écrans. Les adaptateurs sous-jacents restent
responsables des connexions jumelées, des grants, des révisions et des runtimes.
L’API n’expose ni évaluation JavaScript, ni shell arbitraire, ni accès SQL, ni
lecture arbitraire de fichiers, ni jetons de jumelage.

La navigation, les brouillons et les filtres visuels restent de la présentation.
Les abonnements SSE et les lectures détaillées de la réplique restent internes ;
les commandes de synchronisation, d’outbox et `cache.summary` exposent leur état.
Les anciens adaptateurs de téléchargement personnalisé et de démarrage avec un
plan fourni restent accessibles en mémoire seulement. Le parcours actuel de
planification locale utilise le contrôleur partagé, avec revue avant démarrage.

## Transport iPhone de développement

Le listener Network.framework/TLS est compilé seulement avec `DEBUG`. Son
activation nécessite une configuration éphémère injectée au lancement, contenant
une identité TLS et un secret aléatoire. Aucun secret ne traverse le bridge JS.
Les builds Release ne contiennent pas le listener et répondent désactivé aux
méthodes natives d’automatisation.

Le téléphone doit garder l’application au premier plan. Le passage en arrière-plan
ferme le listener et les connexions. La reprise permet une réouverture pendant la
durée restante de la session ; elle ne renouvelle pas son expiration. Pendant une
session Debug prête et au premier plan, le verrouillage automatique est suspendu.
Sa valeur antérieure est restaurée à l’arrêt, à l’expiration ou au passage en
arrière-plan ; l’API ne force jamais le retour de l’application au premier plan. Les choix
de fichiers, permissions, photos et composeurs iOS peuvent nécessiter une
interaction physique. L’API conserve les autorisations existantes.

Routes HTTPS, toutes authentifiées :

| Route | Résultat |
| --- | --- |
| `GET /v1/health` | Disponibilité du bridge, identifiant d’instance et capacité |
| `GET /v1/catalog` | Catalogue versionné des commandes et de leurs paramètres |
| `POST /v1/commands` | Acceptation d’une commande et reçu de traitement |
| `GET /v1/jobs/{id}` | État et résultat du traitement dans cette instance |

Une commande contient exactement `instanceId`, `idempotencyKey`, `command` et
`input`. L’identifiant d’instance vient de `health`. La clé d’idempotence désigne
une commande et ses arguments : une répétition retourne son reçu, des arguments
différents produisent un conflit. Un rechargement JS change l’instance et refuse
les anciennes requêtes. Les identifiants de traitements incluent l’instance et
ne peuvent donc pas désigner un autre traitement après redémarrage.

Les traitements sont `running`, `succeeded`, `failed` ou `uncertain`. Une
réponse perdue ou une erreur après envoi ne prouve pas l’absence d’effet. Ne pas
renvoyer avec une nouvelle clé pour « réparer » un résultat incertain. Consulter
le reçu ou l’état autoritaire de la tâche. Les reçus sont en mémoire, sans reprise
durable après redémarrage ; leur disparition ne signifie pas que l’action n’a pas
eu lieu. Les rejets locaux du plan (`invalid_plan`, `invalid_context`) et
les échecs de préparation (`context_unavailable`, `generation_failed`) restent des
échecs explicites : ils ne signifient pas qu’un démarrage a été envoyé. Après un
POST de démarrage sans réponse vérifiable, le résultat demeure `uncertain`. Un résultat trop volumineux peut être omis tout en conservant la réussite
du traitement (`resultOmitted`).

Le protocole accepte au plus huit traitements actifs et conserve au plus 256
clés dans une session. Il refuse de nouvelles commandes lorsque la capacité est
atteinte, plutôt que d’oublier une clé et risquer un doublon. Il borne aussi les
résultats conservés. Le listener borne les connexions, corps, en-têtes et délais,
rejette les requêtes web avec `Origin`, le chunking et les cadrages HTTP ambigus.

## Compilation dédiée

Depuis `mobile/`, préparer un projet généré pour une compilation **Debug** avec
JavaScript embarqué :

```sh
CI=1 npx expo prebuild --platform ios --no-install
node scripts/prepare-automation-build.cjs
cd ios
COCOAPODS_DISABLE_STATS=1 pod install
cd ..
node scripts/prepare-automation-build.cjs
```

Compiler le workspace `monGARSSwarm.xcworkspace`, scheme `monGARSSwarm`, configuration
`Debug`, destination iPhone, avec une signature Development
valide pour l’appareil. Le script prépare uniquement le projet iOS généré et exclut
le launcher Metro de cette compilation. Il force le bundling Debug directement
dans la phase Xcode, après les fichiers d’environnement ; `pod install` peut supprimer
`.xcode.env.updates` sans désactiver ce réglage. Le second passage vérifie cette phase
et refuse un modèle Xcode inconnu. Le JavaScript embarqué utilise `--dev false` :
les outils Expo de développement exigent un serveur Metro et empêchent un démarrage
autonome. Le drapeau natif `automationAvailable`, compilé avec `DEBUG`, active le
bridge indépendamment de `__DEV__`. Il vaut `false` dans les builds Release.
Avant une compilation normale de distribution, régénérer le projet natif avec
`expo prebuild --clean` : ne pas réutiliser ce projet dédié pour une archive Store.
Signer une compilation Release avec un certificat Development ne suffit pas à
activer cette API.

Le profil de signature doit inclure `Background GPU Access`, conformément à
`ios.entitlements` dans `app.json`. Utiliser la signature automatique Xcode pour
régénérer un profil après activation de cette capacité. Un profil géré par Xcode
ne doit pas être forcé dans un projet configuré en signature manuelle. Les anciens
profils de distribution doivent également être régénérés avant une nouvelle
archive ; une signature valide ne prouve pas le support GPU de l’appareil.

Le client `mobile/scripts/iphone-api.py` prépare la session et vérifie les détails
CoreDevice actuels. Consulter `--help` pour le lancement, le catalogue et les appels.
Il vérifie CA, nom TLS et empreinte du certificat avant d’envoyer le bearer.
L’adresse IPv6 du tunnel doit être découverte à nouveau ; elle n’est pas fixe.

## Ajouter un module

1. Définir une commande versionnée, ses données, erreurs, source d’autorité,
   paramètres bornés et exigences de permission.
2. Enregistrer son handler dans le domaine, en réutilisant l’adaptateur propriétaire
   de l’état. Une fonctionnalité sans implémentation reste indisponible.
3. Faire appeler ce handler par l’interface. Éviter une seconde implémentation
   réservée au transport ou aux tests.
4. Tester les paramètres invalides, effets réels, concurrence, annulation et
   changements de connexion pertinents. Une mutation incertaine ne se rejoue pas
   automatiquement.
5. Valider le catalogue et les façades UI, puis exercer le contrat sur appareil
   pour toute fonction native. Une simulation ou un mock ne prouve pas son
   fonctionnement physique.

Vérifications du socle : tests Jest sous `application-api`,
`mobile/scripts/test-automation-server.sh`,
`mobile/scripts/test_iphone_api.py` et
`mobile/scripts/test-prepare-automation-build.cjs`. Les reçus de compilation et
d’essais physiques sont documentés séparément des tests de contrat.

## Travail pendant l’utilisation d’une autre app

Le transport livré ici reste limité au premier plan. Le maintien éveillé ne
confère aucun droit d’exécution en arrière-plan et n’empêche pas un verrouillage
manuel ou un changement d’application. Les agents Ubuntu déjà démarrés continuent
indépendamment de l’interface iPhone.

Pour prolonger un travail local déjà initié par la personne, iOS 26 propose
`BGContinuedProcessingTask`, avec progression visible, annulation et expiration.
Il peut utiliser le réseau et, sur les appareils compatibles, le GPU. Ce mécanisme
ne garantit pas un serveur de commandes permanent. L’accès GPU exige le droit
`com.apple.developer.background-tasks.continued-processing.gpu` et la vérification
de `BGTaskScheduler.supportedResources`.

Le module natif intègre ce mécanisme pour une génération MLX déjà lancée au
premier plan, avec un modèle déjà chargé. Il demande une exécution immédiate
(`.fail`, sans file d’attente différée) et la ressource `.gpu`. Le calcul ne reçoit
le droit de continuer que lorsque le gestionnaire de lancement Apple a fourni
la tâche correspondante. Une admission tardive, un refus ou un appareil non
compatible conservent le comportement de premier plan.

`models.status.backgroundExecution` et l’écran du modèle local exposent :

- `osSupported`, `gpuSupported`, `supported` : capacités du système et de
  l’appareil ; `supported` seul ne prouve pas l’admission ;
- `entitlementGranted` : `null` avant une preuve d’admission, `true` après une
  admission GPU, `false` après un refus de permission du gestionnaire Apple ;
- `active`, `operationId`, `state`, `reason` : état de la tâche finie courante ;
- `outputBytes` : nombre réel d’octets UTF-8 produits par la tâche d’arrière-plan
  admise, sans l’assimiler
  à un nombre de tokens ni fabriquer un pourcentage.

L’annulation/expiration cible uniquement cette opération. L’achèvement attend
la fin du flux et la synchronisation GPU ; le seul événement de statistiques MLX
ne suffit pas. Une génération Core ML/GGUF, une importation ou un chargement ne
bénéficient pas de cette prolongation. Un arrêt forcé par la personne termine
l’application ; les résultats de l’API restent liés à la session JavaScript,
sans garantie de reprise après destruction du processus.

Le listener HTTPS reste arrêté en arrière-plan. Ce changement permet seulement
la poursuite native d’un calcul admis, puis la récupération de son résultat au
retour dans l’app. Le journal de qualification documente séparément la signature,
le support matériel observé et les essais réellement effectués sur iPhone.

Sur l’iPhone 16 Pro testé sous iOS 26.6.1, le build `20260916034417` possède le
droit GPU dans sa signature et son profil, mais le système retourne
`gpuSupported=false`. Le calcul MLX reste donc au premier plan sur cet appareil.
Le passage à l’écran d’accueil a annulé proprement une génération ; une nouvelle
génération a réussi au retour, avec le modèle toujours chargé. L’exécution GPU
en arrière-plan sur un appareil qui l’admet reste à qualifier. Voir les
[preuves physiques](evidence/application-api-2026-09-16.md).

Sources Apple : [tâches longues sur iOS](https://developer.apple.com/documentation/BackgroundTasks/performing-long-running-tasks-on-ios-and-ipados),
[droit d’accès GPU en arrière-plan](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.background-tasks.continued-processing.gpu).
