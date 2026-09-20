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

## Installation et première tentative verrouillée

L’installation sur l’iPhone physique a réussi sans désinstallation ni purge des
modèles. Le lancement par CoreDevice a ensuite été refusé avec l’erreur
`Locked` : iOS ne pouvait pas déverrouiller l’appareil. Aucun redémarrage
automatique ni rejeu de génération n’a suivi ce refus.

Ce blocage initial est conservé dans `qualification-blocked.json`. Le téléphone
a ensuite été déverrouillé et les essais suivants ont pu commencer.

## Essai physique CPU du build 20260916040148

Le lancement a réussi à 04:14:20 UTC. `app.status` confirme le build natif et
configuré `20260916040148`, au premier plan. `models.load` a atteint `ready` en
**4,498 s**, entre 04:14:44.872 et 04:14:49.370. Ce chemin CPU exécute le contrôle
numérique SiLU/GELU avant les poids : le chargement réussi confirme ce contrôle,
mais ne qualifie pas encore le premier passage complet du modèle.

Une génération limitée à huit tokens a été acceptée à **04:14:58.416**. Le statut
à 04:15:22.019 confirme `executionDevice=cpu`, une tâche **effectivement admise**
(`active=true`), `gpuSupported=false`, `entitlementGranted=null` et **0 octet** de
texte produit. L’admission CPU est donc établie ; la production d’un résultat
ne l’est pas.

La commande d’annulation a été acceptée à **04:16:23.205**. Après une perte
d’accès HTTPS, le retour au premier plan a retrouvé la **même instance** et les
deux traitements, génération et annulation, encore `running`. À
**04:24:23.764**, soit **9 min 25 s** après l’acceptation de la génération, le
modèle est `cancelling` et la tâche `expiring`, avec
`reason=user_or_system_cancelled`, `active=false` et toujours **0 octet**.
Une annulation acceptée ne constitue donc pas ici une annulation achevée.
Le processus répond de nouveau : aucun crash n’est établi par cet essai.

**Aucune génération CPU terminée ni continuation productive après Accueil n’est
qualifiée sur ce build.** Le refus d’un nouveau travail en arrière-plan et la
fermeture du listener à la fin normale du calcul restent également à vérifier
physiquement. La perte de connexion observée ne remplace pas ces preuves.

## Profil CPU du build non optimisé

Le profil du processus 26673 contient **11 475 échantillons**, sur une fenêtre
écoulée de **11,394 s**. **98,42 % du temps CPU échantillonné** appartient au
chemin `_qmm_t<_MLX_Float16, 4, 64>`. Les conversions logicielles
Float→Float16 et Float16→Float occupent respectivement **43,43 %** et **34,02 %**
des feuilles échantillonnées. Le calcul de matrice quantifiée progressait donc
sur le CPU pendant cette fenêtre ; ce n’était pas seulement une attente inactive.
Ce profil ne mesure ni le temps total du premier passage, ni une annulation
achevée, et n’exclut pas l’attente d’un autre thread.

Après la collecte, le processus du build `20260916040148` a été arrêté
**explicitement via CoreDevice** pour permettre la suite. Cet arrêt externe
n’est pas une réussite de la commande d’annulation de l’API.

L’en-tête du fichier `model.safetensors`, à la révision épinglée
`cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1` de
[`mlx-community/dolphin3.0-llama3.2-3B-4Bit`](https://huggingface.co/mlx-community/dolphin3.0-llama3.2-3B-4Bit/tree/cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1),
contient **197 tenseurs UInt32** (1 606 290 432 octets) et **451 tenseurs
Float16** (201 136 512 octets), sans tenseur BF16. Seuls la longueur et l’en-tête
JSON de 72 782 octets ont été lus ; aucun contenu de tenseur n’a été téléchargé
pour cette inspection.

Le préprocesseur Clang, exécuté avec les seuls flags de cible/SDK/architecture
des fichiers de réponse Cmlx réels, confirme `arm64-apple-ios17.0`, SDK
`iPhoneOS26.2`, et `__ARM_NEON=1`. Les macros
`__ARM_FEATURE_FP16_VECTOR_ARITHMETIC`,
`__ARM_FEATURE_FP16_SCALAR_ARITHMETIC` et `__ARM_FEATURE_BF16` sont absentes.
C’est une propriété de cette cible compilée, pas une mesure des capacités
matérielles de l’iPhone. Dans le code MLX 0.31.4 installé,
`backend/cpu/quantized.cpp` sélectionne ainsi le QMM scalaire pour ces demi-types,
alors que la spécialisation Float32 de `simd/accelerate_simd.h` permet le chemin
SIMD pour ce format affine 4 bits.

## Correctifs intégrés au build 20260916042733

Le premier build CPU utilisait `-O0` pour Cmlx et `-Onone` pour Swift. La
compilation Debug **20260916041802**, avec `-O3` et `-O`, a échoué dans le
compilateur Swift 6.2.4 (`SendNonSendable`, `ExpoModulesCore.SharedObject.emit`).
L’incrément final du build **20260916042733** conserve Swift `-Onone` et optimise
les noyaux C/C++ avec `-O3`, via le wrapper dédié qui refuse les réglages
contradictoires. Ses dix tests utilisent un faux `xcodebuild` ; ils ne sont pas
une qualification d’inférence. La compilation de cet incrément a réussi, puis
l’app a été installée sans purge des modèles ; les résultats physiques suivent.

Le code intégré promeut maintenant les paramètres Float16/BF16 en Float32
**uniquement sur le chemin CPU**, après le chargement et avant la publication de
l’état `ready`. Les poids quantifiés UInt32 restent inchangés. Il ne conserve
que les noms et tailles des paramètres à convertir, puis évalue et remplace un
tenseur à la fois, du plus grand au plus petit, via les API publiques
`ModelContainer.perform` et `Module.update`. Le cache libérable est vidé entre
les remplacements ; la trace `after_cpu_parameters` marque la fin de cette étape.

Pour cet artefact, la hausse persistante calculée des paramètres est de
**201 136 512 octets, soit 191,82 MiB**, hors activations et cache KV. Un tenseur
source et son résultat peuvent se chevaucher temporairement pendant la
conversion ; aucune borne identique sur l’empreinte totale de l’app n’est
revendiquée. Les scales et biais Float32 font aussi promouvoir l’entrée du QMM
affine en Float32 dans `ops.cpp`, sans déquantifier globalement les poids UInt32.
Le chemin GPU conserve ses types d’origine.

Un contrôle numérique CPU a été ajouté avant les vrais poids : matrice affine
quantifiée **4 × 64**, groupes de 64, quatre bits, testée avec paramètres F16
puis BF16. Il utilise la même fonction de promotion, vérifie l’intégrité exacte
des mots UInt32, les types Float32 et les quatre résultats par rapport à une
somme scalaire indépendante. Le chargement CPU réussi du build `20260916042733`
confirme désormais son exécution physique avant les poids.

La génération conserve désormais le handle du producteur public
`MLXLMCommon.generateTask`, l’annule explicitement, puis attend sa fin sur tous
les chemins après sa création. Le template de message utilisateur et les
paramètres du précédent `ChatSession` vierge sont conservés pour cette API
texte. Un `defer` synchronise ensuite le **stream effectif de l’opération**
avant de libérer les buffers, y compris si la préparation échoue. C’est distinct
du `Stream()` global synchronisé en interne par MLXLMCommon.

La limite d’annulation des kernels subsiste : dans MLXLMCommon 3.31.4,
`TokenIterator` amorce le premier passage, et `next()` programme le suivant
avant d’attendre le token précédent. Le contrôle de `Task.isCancelled` vient
après le retour de `next()` ; les kernels QMM ne le consultent pas pendant leur
calcul. Le profil explique concrètement le coût du chemin demi-précision non
optimisé. Les mesures suivantes qualifient des cas précis du correctif ; elles
ne constituent pas une garantie générale de latence ou de durée en arrière-plan.

## Résultats physiques du build 20260916042733

`app.status` confirme les numéros natif et configuré `20260916042733`. Le
chargement CPU atteint `ready` en **5,137 s** (04:44:04.214–04:44:09.351 UTC).
Les traces passent de **1 807 466 592** octets MLX actifs après les poids à
**2 008 619 104** après la promotion, soit **+201 152 512 octets** mesurés. Le
pic MLX observé est de 2 008 625 248 octets. Ces compteurs MLX ne sont pas une
mesure de toute l’empreinte mémoire du processus.

Au premier plan, la génération renvoie exactement **`API_OK`**, deux tokens,
avec `finishReason=stop`, en **7,327 s** (04:44:26.098–04:44:33.425 UTC). Le
chemin CPU complet fonctionne donc pour ce cas, après promotion Float32.

Trois essais de continuation ont ensuite été distingués :

| Essai | Preuve observée | Limite |
| --- | --- | --- |
| Réponse longue, limite 128 tokens | L’API est en mode natif `continuation`, avec tâche CPU admise et compteur de texte à **149 octets** à 04:45:36.441. La génération termine avec **88 tokens**, `finishReason=cancelled`, après **77,116 s**. | L’ancienne commande Accueil d’`agent-device` a expiré pendant l’attente du runner : aucun succès de cette commande n’est revendiqué. L’expiration système a interrompu la génération ; ce n’est pas une réponse complète ni une annulation API. |
| Bascule explicite vers Réglages, limite 32 tokens | 17 observations HTTPS en mode `continuation` sur **41,151 s**, nouveau travail refusé **409 / foreground_required**, puis fermeture du listener. Retour du résultat `cancelled`, **0 token**, texte vide. | Aucun reçu d’annulation manuelle ; l’arrêt a précédé la fin normale attendue. |
| Comptage, limite 512 tokens, destiné au test d’annulation | Bascule vers Réglages, 20 observations HTTPS sur **49,078 s**, nouveau travail refusé **409 / foreground_required**, puis fermeture du listener. Résultat `cancelled`, **1 token**, texte vide. | L’opération a été interrompue avant l’envoi de l’annulation prévue ; `cancelReceipt=null`. **Ce test ne qualifie pas l’annulation par l’API.** |

Le résultat long avait été accepté à 04:44:45.056 et terminé à 04:46:02.172 UTC.
Le statut suivant retrouve le modèle `ready`, tâche inactive `cancelled`, raison
`user_or_system_cancelled`. Les retours au premier plan conservent la même
instance API ; les reçus des générations et les deux preuves de réactivation
confirment cette continuité. Les fermetures HTTPS sont ici associées à des
interruptions, pas à une génération menée normalement à son terme en
arrière-plan. **L’annulation API achevée et la fin normale en arrière-plan
restent non qualifiées.**

## Progression déterminée : build 20260916045505

Le compteur système précédent utilisait `totalUnitCount=-1` et les octets de
texte comme unités achevées : la fraction restait indéterminée, à zéro. Cela ne
reflétait notamment pas un token spécial achevé sans fragment de texte visible.
Apple explique que le système s’appuie sur la progression déclarée et peut
expirer une tâche qui n’en rapporte pas. Ce défaut est compatible avec les
interruptions observées ; il ne prouve pas à lui seul leur cause exacte.
[Apple, WWDC25 — Finish tasks in the background](https://developer.apple.com/videos/play/wwdc2025/227/).

Le build **20260916045505** remplace ce suivi par des unités de
travail effectivement réalisées : une unité après la préparation de l’entrée,
puis une par appel d’itérateur ayant rendu un token, même sans texte décodé.
Le compteur est borné et monotone, avec contrôle de l’identifiant d’opération
pour ignorer les notifications tardives. Le total initial vaut `maxTokens+2` ;
la dernière unité est réservée à la jonction du producteur et au drainage du
stream. En fin normale anticipée, le total est ajusté au travail réellement
achevé ; une annulation ou expiration ne devient pas un succès. Les octets de
texte restent un compteur distinct. Aucune progression n’est déduite du temps
écoulé.

La reprise du 20 septembre a récupéré le journal terminal de l’essai :
**16 observations en arrière-plan sur 38,779 s**, toutes à zéro octet, puis
fermeture du listener et résultat `finishReason=cancelled`, zéro token, dans la
même instance. Le nouveau travail avait bien été refusé. L’assertion de
progression productive a échoué : **aucune fin normale en arrière-plan ni
annulation manuelle API n’est qualifiée**. Les anciens reçus et la console native
dans le dossier temporaire ne sont plus disponibles ; les empreintes ci-dessous
constituent le relevé historique, pas une nouvelle vérification de ces fichiers.
Voir la [reprise du 20 septembre](iphone-background-cpu-2026-09-20.md).

Les reçus privés sont dans le dossier désigné par
`/private/tmp/swarmer-cpu-evidence-pointer`. Aucun secret de session n’est publié.

| Reçu | SHA-256 |
| --- | --- |
| `build-cpu-receipt.json` | `baa11aa0a869b26d6e599bfc264a5bbb9e4100cb03863da54e78b6ccb4c65b30` |
| `installation-result.json` | `ef3b719fd1010cc2e255a6ea5dbfc4358d658ddd47781cc5d9d3acd446360013` |
| `qualification-blocked.json` | `f208875d3456517c152a4dce7a21c6351486f4ebb59b2f02fee763da06082866` |
| `cpu-app-status-result.json` | `ed13c847ee832968950fab33362691192684ebc6ea204fa66992a8ec35122e68` |
| `cpu-model-load-result.json` | `09d832ea192bfe01c60377fd3e9ffc55c645615d66c0d89bd5d9cddb1562d0d8` |
| `cpu-smoke-status-result.json` | `57aa46411199ff22d1f37dc4f0418a1ba748a7b64c6f4b6f91e3e55d356a6003` |
| `cpu-recovered-status-result.json` | `1d450433fb1ce3a70faa69294376eec831c8e73ce2d91271b4d48b2f8a79a848` |
| `cpu-smoke-generation-recovered.json` | `ad1090f36458533e228db65a9463ebad14a907dc7181e1a79b6d45924dc9d93f` |
| `cpu-smoke-cancel-recovered.json` | `5e4372c5b98d54c3dd5568f795f9882d4b4a6d7e971d4d489efc80ab416ca5bc` |
| `recovered-health.json` | `a9211fd7643fc7002ee36ad1ed72671402cc6c6e5052738852435790ebe8bb4c` |
| `unoptimized-cpu-profile-summary.json` | `50d33adce09df240fd66979c6bdb81fc1ed73f1745eaa5ec93e0d96af040ad80` |
| `cpu-weight-dtype-metadata.json` | `348c5aff3c641c772c3c3de0491177d8e331a287c5e40d4ea6d782b2b0e4c410` |
| `cpu-compiler-feature-macros.json` | `3e02c6a516a0d62192b4e2023ebc98e2a8a5856df58f0101da8c2054ed156a82` |
| `optimized-compiler-settings.json` | `69d2d88f2725f07069a964e2c1bcadfe5915ad6b865d9d006b01be9bc8c7747d` |
| `optimized-installation-result.json` | `976011922ed2cf684434b11531f87b8982b15f99f2a6fc647b9ff7ec8d6e8d8c` |
| `optimized-app-status-result.json` | `bbeb3bfc90f08418f77a89ceaf256f52788c893f8fcd05c4e2b245429d8bf18b` |
| `optimized-model-load-result.json` | `b40c899374877adcf0df69d14ce7ea549e8b0c81872866e80cc15551e0462ab7` |
| `optimized-load-memory.log` | `8de7b8d2aa1b403574c17b675ee6a7d0ecfbf9411548e206598a56536dc2af74` |
| `optimized-smoke-generation-result.json` | `e62216dd85bcdeff1a5b033457236aa53a7dfed35a411feb58aab814f1b297a7` |
| `optimized-home-inflight-status-result.json` | `434d254a53eb68f0b057054bcde273831746bc6c7216859a0a3d4e9a28c0293c` |
| `optimized-cpu-complete-generation-result.json` | `7ee5a6ca01b205f84fd15da6b2c840c86cb935c65ccdbe2f34a4db6738e645c6` |
| `optimized-after-system-cancel-status-result.json` | `d53abab31833b54170a1f0b8bc1cd4fe4b9fce86c195576d58bf2d53235e6b6a` |
| `optimized-direct-complete-proof.json` | `955b81567c76f8bab3ac78b2d6834407acb2b3282fd815592b97442b793356af` |
| `optimized-direct-complete-blocked-new-work.json` | `eb879f9c4c7bfa3c10c3d6bf117c1ef0b174297930ffc125c04e3359ef0bbb63` |
| `optimized-short-cancel-proof.json` | `f3dc5960d3a871111f95ec0a602d2c4b19ae6b63cc13de36ecc39c493bbb7f35` |
| `optimized-short-cancel-blocked-new-work.json` | `eb879f9c4c7bfa3c10c3d6bf117c1ef0b174297930ffc125c04e3359ef0bbb63` |
