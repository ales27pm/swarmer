# API applicative iPhone — qualification du 16 septembre 2026

## Contrat et portée

Le registre exécutable expose 67 commandes uniques avec schémas d’entrée et
descripteurs de sortie. Les actions métier de l’interface utilisent les mêmes
handlers, services de revue et contrôleurs. La disponibilité d’un handler ne
prouve pas que son serveur, modèle ou autorisation est prêt.

Le transport HTTPS est réservé aux builds natifs Debug, activé par une session
éphémère au lancement et arrêté en arrière-plan. Les données et autorisations
Ubuntu restent autoritaires. Les sélecteurs et composeurs iOS peuvent nécessiter
une interaction système. Aucune commande de shell, SQL ou fichier arbitraire
n’est exposée.

## Vérifications du code

- Dernière suite mobile complète : **44 suites, 635 tests réussis**, après l’ajout du statut
  d’arrière-plan et les corrections de propriété des générations.
  Le reçu est `/private/tmp/swarmer-bg-jest.log` (41,496 s).
- Les 98 tests ciblés de ces erreurs, TypeScript et lint passent également.
  Le premier jalon, avant le correctif de démarrage, comptait 589 tests réussis.
- Correctif de démarrage : cinq tests du bridge, trois tests de préparation du
  build au premier jalon, TypeScript et lint ciblé réussis. Le correctif final
  de bundling passe **5/5 tests de préparation**, y compris la suppression du
  fichier d’environnement par CocoaPods.
- Arrière-plan MLX : **12/12 tests natifs** du contrôleur et du cycle de vie
  réussis ; typecheck Swift 6 intégré réussi. Les branches simulées d’admission
  ne remplacent pas un essai GPU en arrière-plan sur matériel compatible.
- Natif : protocole/authentification, véritable HTTPS sur loopback, reprise et
  expiration, typecheck Swift 6 strict pour arm64 iOS 18 ; exclusion du listener
  et du parseur dans le binaire Release du harness.
- CLI Mac : 41 tests réussis sous Python 3.9 et 3.12 ; Ruff format/check réussis.
  Ils couvrent notamment TLS avant bearer, session privée, instance liée,
  lancement unique, changement d’adresse du même appareil avant liaison,
  supervision de la console et absence de répétition automatique d’un POST.
- Les tests sur mocks et loopback ne constituent pas des essais sur iPhone.

## Compilation et installation

Xcode 26.3, SDK iOS 26.2, compilation native Debug signée Development pour
l’iPhone 16 Pro appairé. App `org.27pm.mongars`, version 0.1.0, build de base
compilé et installé **`20260916031957`**. La mise à jour a conservé les fichiers
du modèle. Les essais sont explicitement rattachés à leur build ; ce jalon
précède l’intégration des tâches MLX en arrière-plan.

La première version `20260916022500` s’installait mais ne démarrait pas son domaine
JavaScript : Expo tentait une connexion devtools incompatible avec un bundle
embarqué. La capture de l’utilisateur a permis d’identifier ce blocage.
Le build `20260916024500` a corrigé ce démarrage avec `--dev false` pour son
JavaScript et le drapeau natif `automationAvailable` pour distinguer Debug et
Release. Le build `20260916030500` ajoute le correctif mémoire natif décrit
ci-dessous ; `20260916031500` ajoute la classification explicite du plan local
invalide ; `20260916031957` corrige la durée de vie des générations et du modèle
partagé ainsi que le maintien éveillé pendant une session API au premier plan.
Ces builds de développement ne constituent pas une soumission
TestFlight.

- `BUILD SUCCEEDED` ; signature stricte vérifiée.
- Sept images Mach-O vérifiées, aucune dépendance requise manquante ni bibliothèque
  de tests liée.
- Les 124 fichiers applicatifs suivis pour cette compilation étaient inchangés.
- Bundle JS final : **4 819 537 octets**, SHA-256
  `56274207a48d0b3dc17cc2e3fc089ecad0693fb428b8452aa181cfef5788d8a7`.
- Au jalon précédent, la mise à jour physique vers `20260916030500` avait réussi,
  sans désinstallation ni purge des modèles. Son `app.status` confirmait les
  numéros natif/configuré, l’état actif et l’inférence locale disponible.
- Le commit natif `5da321412daefb20a656cd31dc4449a1f8e0c1cb` est enregistré.
  Les reçus de publication Git sont distincts des preuves de compilation.

## Transport et essais de sécurité physiques

Le premier lancement a mis en évidence une difficulté réelle : CoreDevice
pouvait libérer son tunnel ou changer son adresse après la sortie de la
commande de lancement. Le CLI propose maintenant `launch --console
--device-tunnel` : un seul lancement maintenu au premier terminal, puis
redécouverte de l’adresse du même appareil avant HTTPS. Les commandes sont
envoyées depuis un second terminal. Aucun rejeu automatique de lancement ou
d’action métier n’est utilisé.

La vérification TLS contrôle la CA, le SAN et l’empreinte DER avant d’envoyer le
bearer. La session est ensuite liée à l’instance JavaScript retournée par
`health`. Les éléments suivants ont été vérifiés sur le téléphone avec le build
**`20260916031500`** :

- mauvais bearer : HTTP 401 ;
- mauvaise empreinte TLS : requête rejetée ;
- même clé d’idempotence et mêmes arguments : même job retrouvé ;
- même clé avec arguments différents : HTTP 409 ;
- ancienne instance : HTTP 409.

Le reçu `verified-negative-tests.json` réexécute ces cinq cas sur le build
31500. Les cinq vérifications ont aussi été exécutées avec succès sur le build
final **34417** (`background-final-negative-tests.json`). Le catalogue lu physiquement
contient **67 commandes** ; leur présence ne signifie pas que les 67 fonctions
ont été exercées sur l’iPhone.

La console est bornée par la durée de session. Pour éviter la transmission de
SIGTERM/SIGINT par `devicectl` à l’app, le CLI ne ferme que son enfant local avec
SIGKILL lors d’une interruption ou de l’expiration. Il ne commande pas l’arrêt
du processus iPhone. La survie de l’app après cette fermeture de console reste
une limite distincte à qualifier ; aucun service de maintien permanent n’est
installé.

## Régression mémoire et correction MLX

Les essais de chargement antérieurs ont produit des rapports Jetsam indiquant
`per-process-limit`, autour de **3 376 MiB** pour le processus de l’app. Il ne
s’agissait donc pas seulement d’un échec de connexion du client API.

Une reproduction isolée sur Mac a confirmé que la lecture Foundation par blocs
pouvait conserver des objets temporaires pendant la vérification SHA-256.
Sur un fichier de **512 MiB**, avec des blocs de **4 MiB**, le pic d’empreinte
physique échantillonné était d’environ **513 MiB** sans vidange locale, contre
**4,8 MiB** avec un `autoreleasepool` par bloc ; le digest était identique.
Ce résultat mesure le harness Mac, pas le pic total de l’app iPhone.

Le correctif applique ce pool aux boucles de copie et de vérification du
stockage durable. Le chargement MLX borne également son cache réutilisable à
**20 MiB**, le vide aux étapes prévues, et charge le tokenizer avant les poids
afin d’éviter leur chevauchement. La limite du cache n’est pas une limite de
20 MiB sur la mémoire totale du modèle.

Le nouveau test de rétention échoue sur l’ancien stockage : environ **92 MiB**
conservés pendant la copie et **128 MiB** pendant la résolution, au-delà de la
borne de 64 MiB par phase. Après correction, les **17/17 tests** passent ; les
croissances mesurées sur cette fixture sont respectivement de **28 KiB** et
**56 KiB**. Les tests de hash sur plusieurs blocs et de corruption du dernier
octet restent réussis.

## MLX et inférence acquises sur le build 20260916030500

L’inventaire applicatif confirme **1 824 808 562 octets** de fichiers MLX durables
sous `Documents/Models` pour Dolphin 3B. L’installation de mise à jour a conservé
ces fichiers ; cette taille décrit le stockage, pas la mémoire résidente.

Les résultats physiques suivants concernent **`20260916030500`** ; ils ne sont
pas présentés comme une nouvelle qualification de `20260916031500` :

- `models.load` a terminé avec `state=ready`, `runtime=mlx`, en **4,225 s** entre
  acceptation et fin du job ;
- après retour au premier plan, le même processus et la même instance ont été
  conservés ; un GET a retrouvé le job de chargement réussi sans réémettre
  l’action ;
- `inference.generate` a réellement produit **`API_OK`**, **2 tokens**, avec
  `finishReason=stop`, en **0,859 s** entre acceptation et fin du job ;
- une récupération explicite avec la même clé a retourné `replayed=true` et le
  job original inchangé, sans nouvelle génération.

Ces mesures prouvent un chargement et une courte inférence locale réussis.
Elles ne constituent ni un benchmark soutenu, ni une garantie d’absence de
Jetsam pour tous les modèles, prompts ou enchaînements d’actions.

## Scénario CRM

Sur le build `20260916030500`, `goals.create` puis `goals.plan.prepare` ont réussi.
Le GET de contrôle retrouve le but en **`planning`**, avec **0 nœud**, **0 étape**
et **0 appel de modèle enregistré par le control plane**. Cela atteste la
création du but et la préparation du contexte, pas la création d'une application
CRM ni un démarrage Ubuntu.

Le diagnostic réel du plan local a retourné une chaîne **vide**, avec
`finishReason=stop` et **1 token** (`bounded-crm-raw-result.json`). Un diagnostic
de clôture après un rechargement d'environ quatre secondes a également rendu
une sortie vide. L'app est restée vivante ; aucun nouveau Jetsam n'a été observé
pendant ce diagnostic. Le succès du transport ou de l'appel d'inférence ne
signifie donc pas qu'un plan utilisable a été produit.

Le correctif intégré au build `20260916031500` classe cette sortie en
**`failed` / `invalid_plan`**, avec un message explicite indiquant qu'aucun plan
n'a été accepté ni démarré. Un démarrage POST dont l'issue est inconnue conserve
distinctement l'état **`uncertain` / `outcome_unknown`** et ne devient pas un
échec certain autorisant un renvoi automatique. Ces distinctions sont couvertes
par les tests de code ; leur preuve physique finale reste à renseigner.

Sur 31500, la planification a ensuite rencontré `model_not_ready` : une fermeture
d’écran pouvait libérer le modèle utilisé par l’API. Le build 31957 supprime cette
libération automatique et réserve l’annulation de l’écran à la génération dont
il est propriétaire.

Une lecture autoritaire ultérieure du même but, sur 31957, le trouve en
`budget_exhausted`, `planner_source=ubuntu_local`, avec six nœuds de synthèse,
zéro agent utilisé, cinq nœuds bloqués et un ignoré. Son démarrage serveur est
daté de 03:12:09 UTC ; il ne provient pas d’un plan iPhone accepté par les essais
ci-dessus. Le nouveau `goals.plan.prepare` rejette correctement cet état déjà
démarré (`invalid_state`). Aucun CRM fonctionnel ni plan local validé n’est établi.

## Qualification physique supplémentaire du build 31957

- `app.status` confirme les numéros natif et configuré ainsi que l’état actif.
- `models.load` charge Dolphin MLX en **4,516 s**, avec `state=ready`.
- `inference.generate` retourne **API_OK**, **2 tokens**, `finishReason=stop`,
  en **0,469 s**.
- Après un passage hors du premier plan et réactivation, la même instance API
  est retrouvée et `models.status` conserve le modèle en état `ready`.
- Une connexion perdue n’a déclenché aucune répétition automatique de commande.
  La préparation a été récupérée manuellement avec sa clé d’idempotence initiale.
- Ce jalon ne prouve pas encore une génération GPU en arrière-plan ni la survie
  de l’app après fermeture de la console de développement.

## Intégration MLX en arrière-plan

Le contrôleur iOS 26 demande une tâche `BGContinuedProcessingTask` finie,
immédiate (`.fail`) avec `.gpu`. Il conserve seulement une génération MLX
commencée au premier plan et effectivement admise. Son progrès correspond aux
octets UTF-8 produits ; achèvement et annulation attendent la synchronisation
GPU. Le statut natif est visible dans `models.status.backgroundExecution` et
l’écran du modèle. Le listener API reste limité au premier plan.

La relecture a corrigé les callbacks de lifecycle retardés : ils réconcilient
l’état courant d’iOS et ciblent l’opération concernée. Une annulation survenue
après les statistiques MLX ne retourne plus un faux `finishReason=stop`.
Les tests du contrôleur et le typecheck Swift intégré passent ; ces tests ne
prouvent pas une admission GPU sur le téléphone.

Le POST public App Store Connect de la capacité a été refusé (`409`, type
inconnu). La voie officielle Xcode a ensuite activé `BACKGROUND_GPU_ACCESS`,
créé un profil contenant le droit GPU et signé une sonde avec le certificat
Development existant. L’iPhone est inclus. Cette sonde n’a jamais été installée
ou lancée. Le nouveau profil expire le 16 septembre 2027. Son empreinte est
`32030c3b9e67c6b943a5853efb5c7e47560707d66d3d878a45554ac25f39b117`.
Les quatre anciens profils ont été invalidés par Apple lors du changement de
capacité ; aucun n’a été supprimé. Les prochaines archives de distribution
nécessitent un profil régénéré.

### Build final 20260916034417

Cette version Debug est compilée, signée et installée sur l’iPhone physique,
sans désinstallation ni suppression des modèles. `app.status` confirme le même
numéro pour le binaire natif et la configuration JavaScript.

- `BUILD SUCCEEDED`, signature stricte vérifiée, droit GPU présent dans le
  binaire et le profil ; appareil inclus dans ce profil.
- **128 fichiers sources inchangés** pendant la compilation ; audit des sept
  images Mach-O réussi.
- JavaScript embarqué `--dev false`, **4 827 025 octets**, SHA-256
  `76744d02aeb2e354347b9bc1c11794c00bf43b3c3c5b59f85d88947bf268bb76`.
  Les marqueurs du statut d’arrière-plan sont présents dans le bundle Hermes.

La tentative intermédiaire 33113 avait conservé l’ancien JavaScript :
`pod install` supprimait `.xcode.env.updates`, réactivant `SKIP_BUNDLING`.
L’audit a détecté ce bundle périmé. La préparation force maintenant le bundling
Debug après tous les fichiers d’environnement, directement dans la phase Xcode.
La version 33113 n’est pas une qualification de cette intégration.

### Résultats physiques du build final

Sur cet **iPhone 16 Pro, iOS 26.6.1**, le contrôle natif retourne
`osSupported=true`, **`gpuSupported=false`**, `supported=false` et
`reason=gpu_unsupported`. La signature autorise la capacité, mais le système ne
propose pas la ressource GPU en arrière-plan. `entitlementGranted=null` reste
correct : aucune admission de tâche n’a eu lieu. Ce résultat concerne cet
appareil et cette version d’iOS, pas tous les appareils Apple.

- Chargement du modèle MLX durable : **4,512 s**, puis `state=ready`.
- Génération locale : **API_OK**, **2 tokens**, `finishReason=stop`, **0,464 s**.
- Une génération longue a été lancée une seule fois par l’API, puis le bouton
  système Accueil a réellement été actionné par le runner signé. Le listener
  API était fermé en arrière-plan.
- Au retour dans l’app, un GET du même job, dans la même instance, a retrouvé
  **21 tokens** et **`finishReason=cancelled`**. Le job de transport est terminé
  avec succès parce qu’il a renvoyé ce résultat ; le calcul demandé a été annulé.
  Aucun nouveau POST n’a été envoyé pour reprendre cette génération.
- Une nouvelle génération au retour produit **API_OK**, **2 tokens**,
  `finishReason=stop`, en **0,300 s**. Le statut final confirme que le même modèle
  MLX est toujours `ready`, avec `state=foreground_only` pour l’arrière-plan.
- Authentification, empreinte TLS, rejeu idempotent, conflit de paramètres et
  ancienne instance ont été revérifiés sur ce build ; les 67 commandes sont
  présentes et uniques.

L’annulation et la reprise au premier plan sont donc qualifiées sur le téléphone.
Une génération GPU admise en arrière-plan, sa progression et son expiration
restent à tester sur un appareil qui expose cette ressource. Ce build ne rend
pas l’API joignable en arrière-plan et n’a pas été soumis à TestFlight.

## Traçabilité des preuves

Les reçus physiques privés sont dans le dossier désigné par
`/private/tmp/swarmer-api-evidence-pointer`. Aucun jeton, fichier de session,
adresse réseau ou identifiant privé n’est reproduit ici.

| Preuve | Fichier et empreinte SHA-256 |
| --- | --- |
| Compilation finale 34417 | `build-background-receipt.json` — `33fb5920d67740c6a29ccd5383f3ad7e9eb702601989c2db86ae119733b85908` |
| Support GPU observé 34417 | `background-app-status-result.json` — `6971412dcdb630ddf63f5399acdcb9c8218c065cbe1c7742ad55c6e5a292177d` |
| Chargement MLX 34417 | `background-mlx-load-result.json` — `8acc8c402ec9e5facb682ef65ff66092266df82f7d88398e9d3916a3fdec8065` |
| Génération MLX 34417 | `background-generation-result.json` — `78e70e452cd771b31693f8a7da9a2a819ff3c2e829b8dbbf9cd4040fe90754b7` |
| Accueil et annulation réelle 34417 | `background-interruption-proof.json` — `63d364a789d7fc5bd23f1556585ea807c3c5a12775d92f15881be1863fa3978e` |
| Génération au retour 34417 | `background-generation-after-return-result.json` — `f3b9d340999dbe84f5a5a8657df13e6119cb850d300b6aa0656f5847a6c70ebb` |
| Modèle conservé au retour 34417 | `background-final-model-status-result.json` — `ab4707e99a520f2f3dd5c07770cc09245d6da3e4725b8409bef1b9c890816cb3` |
| Tests négatifs 34417 | `background-final-negative-tests.json` — `310e29b130cd9e19896d81e93facb96ad6bfa4d9109911b7dae8814549bd3990` |
| Compilation 31957 | `build-qualified-receipt.json` — `880dff59c474d0ca3a8d4864adccc8c4df2b03b2cd8005cea76f99e6bd95db77` |
| Tests négatifs 31500 | `verified-negative-tests.json` — `8190a7c4f364d7139d7bc589301fd463874126cb63c9320ba2c40719001c44de` |
| Chargement MLX 31957 | `qualified-mlx-load-result.json` — `43b0d1f9411395c5e1573dce70e98afc2dbe52c69b24cae1f76aabbdf68c58a2` |
| Génération MLX 31957 | `qualified-generation-result.json` — `53e905994fe6397b8b9491b816a89b4ea15dabee375e24411c2b5c7115f7e745` |
| Modèle conservé 31957 | `qualified-model-retained-result.json` — `4b6f1f7e2df218d5e5de7bf561dccf57de7bcde2584d3636c0e12cdea6e41743` |
| État CRM autoritaire ultérieur | `qualified-crm-state-result.json` — `e84b82678f829b7b9d46cb6ca7ce101e00438bd4cac16d68e04a83b631af80e4` |
| Compilation 31500, signature et dépendances | `build-final-receipt.json` — `11eb5de4efbe925c003438f4895aee07b61ad4578141eae96fc0871d78c686e8` |
| Compilation historique 30500 | `build-bounded-receipt.json` — `dea74518552cc6aa047d1197348d84eaa08145a4dd23d0b828430f99dc57acfb` |
| Tests négatifs du build précédent | `physical-negative-tests.json` — `951d36f26460701538ebbdb61ac27ce115535a8cdcde392b1b40469168523130` |
| Chargement MLX 30500 | `bounded-mlx-load-final.json` — `313383864e89ed5511ad194d5ae0bb32d5ade7072cb214d3ae57847d65a941ad` |
| Inférence locale 30500 | `bounded-generation-result.json` — `c079f1523da4453b0594e0578453c46bcabefff6267529b20f50dac25866d889` |
| Reçu rejoué, même job | `bounded-generation-replay.json` — `95897a6d6b134cacc40f624c3a7ce197d5e601c09b45a2303082a1c058677208` |
| Sortie brute du diagnostic de plan CRM | `bounded-crm-raw-result.json` — `a6c966846fd3131c3413519ff3ab2a069a9a0f97e6998980167e67e42a3bd394` |

Les preuves de régression mémoire sont
`/private/tmp/swarmer-filehandle-memory-proof-20260916/result.json`,
`/private/tmp/swarmer-local-model-store-memory-patched.log` et
`/private/tmp/swarmer-store-memory-original.qJ7E2g/result.log`.
