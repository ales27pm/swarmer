# MLX CPU et suivi API en arrière-plan — 16 septembre 2026

## Problème et recherche

Le build précédent `20260916034417` confirmait `gpuSupported=false` sur l’iPhone
16 Pro sous iOS 26.6.1, avec le droit GPU pourtant présent dans la signature et
le profil. Il annulait donc MLX au départ de l’app et fermait l’API.

La recherche Exa a porté sur deux axes : support GPU/MLX et exécution CPU/réseau.
Les cinq recherches ont demandé 26 résultats, avec des doublons ; seules les
sources primaires ci-dessous et le code MLX effectivement compilé fondent le
correctif. Le nombre de résultats n’est pas un nombre de sources indépendantes.

- [DTS Apple : iPhone 16 Pro et GPU en arrière-plan](https://developer.apple.com/forums/thread/797538) :
  la signature ne crée pas une ressource absente de `supportedResources`.
- [Apple : tâches continues](https://developer.apple.com/documentation/backgroundtasks/performing-long-running-tasks-on-ios-and-ipados) :
  une opération finie, lancée au premier plan, peut continuer avec le CPU et le
  réseau, sous admission, progression, annulation et expiration du système.
- [DTS : limites d’exécution en arrière-plan](https://developer.apple.com/forums/thread/685525) :
  pas de serveur permanent général ni de réveil arbitraire par requête entrante.
- [MLX Swift 0.31.4](https://github.com/ml-explore/mlx-swift/tree/0.31.4) :
  les kernels CPU sont compilés sur iOS ; le JIT CPU est exclu. Les scopes Swift
  de device et de stream sont hérités par les tâches filles utilisées par
  MLXLMCommon 3.31.4, à l’intérieur de notre tâche de génération.

## Correctif

Quand iOS 26 ne propose pas le GPU en arrière-plan, le chargement et la génération
MLX utilisent le CPU. Le JIT est désactivé via l’API publique MLX avant ce chemin,
une fois par processus ; aucune valeur globale de device n’est modifiée. Un
contrôle numérique SiLU/GELU sur CPU précède le chargement des poids.

La tâche continue demande `requiredResources=[]` pour le CPU, avec stratégie
`.fail`. Le champ `executionDevice` indique le moteur réellement sélectionné.
`gpuSupported` reste faux et une admission CPU ne devient jamais une permission
GPU. La génération reste entièrement locale.

Le listener HTTPS déjà ouvert peut accompagner ce calcul effectivement admis.
Chaque requête reçoit l’état natif courant, indépendamment d’un événement JS
retardé. GET, `models.status`, `inference.cancel` et récupération de reçus
idempotents restent autorisés. Une nouvelle génération, un chargement ou une
autre mutation requiert le premier plan. La fin du calcul ou son expiration
ferme le listener en arrière-plan ; aucun maintien en attente n’est ajouté.

## Vérifications

- Suite mobile complète : **44 suites, 654 tests réussis**, 40,785 s.
- TypeScript et lint des fichiers modifiés : réussis.
- Contrôleur natif : **14 tests réussis**, avec admission CPU sans permission
  GPU, refus, expiration, annulation et événements de cycle de vie retardés.
- Typecheck natif intégré Swift 6 : réussi.
- Transport natif : protocole/authentification, contrôle de génération et de
  durée de session, HTTPS réel sur loopback avec admission/fin du travail,
  transition pendant le démarrage, exclusion Release et compilation iOS 18.

## Compilation

Build Debug **20260916040148**, `BUILD SUCCEEDED`. La compilation complète a
relevé puis validé la correction de concurrence des closures CPU : elles sont
explicitement `@Sendable`, avec les accès à l’acteur attendus. Un simple
typecheck n’avait pas détecté cette erreur de génération de code.

- Signature stricte valide, profil contenant l’iPhone et le droit GPU.
- 160 fichiers suivis inchangés pendant le build ; sept images Mach-O vérifiées.
- JavaScript embarqué : 4 830 165 octets, SHA-256
  `12f0e5f2aa0ff991a0edfb91338664f59dbb6fd6ed3fc69344678add4fb7f851`.

## Installation et limite de qualification

L’installation sur l’iPhone physique a réussi sans désinstallation ni purge des
modèles. Le lancement par CoreDevice a ensuite été refusé avec l’erreur
`Locked` : iOS ne pouvait pas déverrouiller l’appareil. Aucun redémarrage
automatique ni rejeu de génération n’a suivi ce refus.

**La génération CPU et le suivi HTTPS en arrière-plan ne sont donc pas encore
qualifiés physiquement.** Les essais attendent le déverrouillage de l’iPhone.
Leur scénario prévu vérifie l’admission CPU, la progression après Accueil, le
refus d’une nouvelle génération en arrière-plan, l’annulation par API et la
récupération du résultat dans la même instance.

Les reçus privés sont dans le dossier désigné par
`/private/tmp/swarmer-cpu-evidence-pointer`. Aucun secret de session n’est publié.

| Reçu | SHA-256 |
| --- | --- |
| `build-cpu-receipt.json` | `baa11aa0a869b26d6e599bfc264a5bbb9e4100cb03863da54e78b6ccb4c65b30` |
| `installation-result.json` | `ef3b719fd1010cc2e255a6ea5dbfc4358d658ddd47781cc5d9d3acd446360013` |
| `qualification-blocked.json` | `f208875d3456517c152a4dce7a21c6351486f4ebb59b2f02fee763da06082866` |
