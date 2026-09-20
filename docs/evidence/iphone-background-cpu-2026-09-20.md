# Reprise de la qualification MLX CPU — 20 septembre 2026

Le build installé `20260920220700` a réussi deux essais bornés de continuation
MLX sur CPU : une annulation explicite par l’API et une génération atteignant
son budget de 16 tokens. Une première sortie a été observée au premier plan avant
le changement d’app ; le texte a ensuite progressé entre observations en
arrière-plan. Cette qualification ne couvre ni la préparation initiale sans
sortie, ni les longues durées, ni l’écran verrouillé. L’échec du 16 septembre
reste un résultat historique distinct.

## Résultat récupéré

Le journal `/private/tmp/swarmer-cpu-progress-complete-run.log` du build
`20260916045505` contient 16 observations avec accès natif `continuation`,
`executionDevice=cpu`, tâche admise, mais zéro octet produit. Après 38,779 s
d’observation en arrière-plan, le listener ferme. Le retour au premier plan
retrouve la même instance et un reçu de commande `succeeded` contenant
`finishReason=cancelled`, zéro token et texte vide. Le statut de commande ne
signifie donc pas que la génération a abouti normalement. L’assertion de
progression du texte a correctement échoué. Aucune annulation API n’a été envoyée.

Les fichiers temporaires détaillés de cet essai ont disparu. Le journal terminal
subsiste ; il ne permet pas de retrouver la fraction native de progression ni
la cause exacte de l’expiration.

SHA-256 du journal conservé :
`d3a7a2142802177983efdde1b10782801fc299e092cd8342914ee5f073b1570e`.

## Correctif supplémentaire

Le code MLX 0.31.4 compilé conserve un worker CPU par nouveau stream dans son
scheduler. Le précédent runtime appelait `Stream.withNewDefaultStream` à chaque
chargement et génération. Il réutilise désormais le stream CPU par défaut,
adapté à cet unique propriétaire qui sérialise les opérations du modèle.

`Device.withDefaultDevice(.cpu)` reste scopé. Une garde vérifie le device et
`StreamOrDevice.default.stream == Stream.cpu` avant tout calcul CPU : un stream
TaskLocal ambiant incompatible est refusé. La jonction du producteur et le
drainage du stream avant libération des buffers sont conservés. Cette correction
empêche l’accumulation de workers ; elle n’est pas une preuve de la cause des
expirations antérieures.

Sources du paquet installé : `MLX/Stream.swift` (priorité du TaskLocal),
`Cmlx/mlx/mlx/scheduler.h` (`new_stream` et workers conservés),
`Cmlx/mlx-c/mlx/c/private/stream.h` (destruction du handle).

## Vérifications de la reprise

- 10 tests du wrapper de compilation : réussis.
- 20 tests natifs du cycle de vie et de la progression : réussis, compilation
  Swift 6 avec concurrence stricte et avertissements traités comme erreurs.
- Typecheck du contrôleur pour iOS 18 : réussi.
- Tests du transport API : réussis, avec HTTPS réel, certificat épinglé,
  authentification, contrôle d’accès natif actualisé, fermeture à la fin de la
  tâche ou du TTL, exclusion Release et typecheck Swift 6 iOS 18.
  Journal `/private/tmp/swarmer-background-20260920-transport.log`, SHA-256
  `c8965a11ab48e4e213a0fe5bac8ab7e746d3e7d012f7ceef46ea5e5a9778f99b`.
- Build Debug `20260920220700` : compilation et audit réussis.

### Compilation signée

La première compilation avait été interrompue à l’étape de signature avant la
pause. La reprise sous le compte utilisateur, après déverrouillage du trousseau,
a produit le build `20260920220700`. Les 140 fichiers source de l’application
contrôlés n’ont pas changé pendant la compilation. La vérification stricte de
signature réussit ; l’app est signée pour le développement et son profil inclut
l’iPhone visé. L’audit des dépendances des sept images Mach-O réussit sans anomalie.

Le droit GPU figure dans la signature et le profil ; cela ne prouve pas que
l’appareil autorise le GPU en arrière-plan. La capacité doit toujours être lue
sur l’appareil lors des essais.

Le reçu conservé est
`/Users/ales27pm/Library/Developer/Xcode/SwarmerAPIQualifications/20260920220700/build-receipt.json`,
SHA-256 `521c6b6eeec50a544fa666cac14eccdbceb6b76c6a51196e10621d4defc5dd6a`.
Il référence le journal de compilation, SHA-256
`c6ab0890a09c02010cd9475b9130d21efe99afa662e072b39090a9de552b3aa0`,
et l’IPA, SHA-256
`2b016140c9195d5c8d2cdf0e33e5945822fdc1520203a0bd0181d1325fe001ed`.

Le reçu de compilation a été établi avant la confirmation de l’installation.
L’installation est depuis confirmée : `app.status` à 23:24:39 UTC rapporte les
numéros natif et configuré `20260920220700`. Les résultats physiques obtenus
ensuite sont décrits séparément ci-dessous.

## Connexion physique

Les outils lancés sous le compte macOS de la session retrouvent l’iPhone 16 Pro
appairé ; le diagnostic initial exécuté sous root ne le voyait pas. Les détails
CoreDevice signalaient initialement iOS 26.7, transport réseau local, mais tunnel
déconnecté et services développeur indisponibles. Le service réseau de jumelage
est joignable.
Une relance ciblée des services utilisateur CoreDevice/RemotePairing n’a pas
rétabli le tunnel. Aucun jumelage ni réglage réseau n’a été supprimé.

### Connexion rétablie le 20 septembre

Le relevé de 23:16:48 UTC confirme désormais l’appairage, l’état `booted`, le
transport `localNetwork`, le tunnel IPv6 connecté sur TCP et
`ddiServicesAvailable=true`. Les capacités installation, lancement, contrôle de
processus et transfert de fichiers sont présentes. Le relevé de 23:18:09 UTC
confirme `passcodeRequired=false` et `unlockedSinceBoot=true`.

Les journaux CoreDevice montrent le montage DDI réussi, puis une fermeture du
tunnel dix secondes après la fin de la dernière assertion d’usage, avec la
raison `No active usage assertions`. Ce tunnel s’établit à la demande : sa
fermeture au repos ne signifie donc pas une perte d’appairage. Cette observation
est distincte de l’échec historique de génération en arrière-plan décrit plus
haut.

Preuves locales conservées :

- `/private/tmp/swarmer-iphone-latest-details.json`, SHA-256
  `686206c91a00ad16e4a31626b0b5f4c9d63d0ffea6e90dd8d2552beb5164a949`.
- `/private/tmp/swarmer-iphone-ready-lockstate.json`, SHA-256
  `cec71d0728981db22c701966a5c9a2ea5a2c88894cb81176232080fc9dd16404`.

## Qualification physique du build installé

Sur l’iPhone 16 Pro sous iOS 26.7, `app.status` confirme `gpuSupported=false`.
Les essais utilisent `executionDevice=cpu`, avec tâche admise et accès natif
`continuation`. Le modèle Dolphin MLX de 1 824 808 562 octets est conservé dans
`Documents/Models`, dépôt `mlx-community/dolphin3.0-llama3.2-3B-4Bit`, révision
`cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1`. `models.load` passe à `ready` en
5,435 secondes.

Les deux essais contrôlés attendent une première sortie au premier plan avant
le changement d’app. La progression retenue est mesurée entre observations en
arrière-plan, et non seulement depuis cette première sortie.

| Essai | Progression en arrière-plan | Reçu final |
| --- | --- | --- |
| Annulation par API | 3 observations, de 28 à 31 octets | `inference.cancel` réussi ; génération `finishReason=cancelled`, 7 tokens |
| Génération suivante, budget de 16 tokens | 20 observations, de 31 à 71 octets ; 28,868 s d’observation | `finishReason=length`, 16 tokens, sans annulation |

L’annulation est acceptée à 23:29:39.902 UTC et terminée à 23:29:41.787 UTC,
soit 1,885 seconde. La génération suivante, acceptée à 23:30:01.861 UTC, termine
à 23:30:42.184 UTC. Elle réutilise le modèle après annulation et atteint son
budget pendant la continuation. Dans les deux cas, le listener ferme à la fin,
un nouveau calcul en arrière-plan est refusé avec HTTP 409
`foreground_required`, et le modèle reste `ready` dans la même instance.
Les reçus finaux sont récupérés au retour au premier plan sans nouvelle
soumission. `succeeded` est interprété avec `finishReason` : l’annulation réussie
n’est pas un achèvement normal de la génération.

Le premier essai de 32 tokens a montré 26 observations en arrière-plan, de 31 à
113 octets, la dernière à 61,233 secondes. L’app est toutefois revenue au premier
plan avant la fin : l’assertion exigeant `continuation` a échoué et le listener
n’a pas fermé pendant l’observation. Son reçu final de 32 tokens avec
`finishReason=length` ne qualifie donc pas l’achèvement en arrière-plan.
Deux autres tentatives de lancement du scénario d’annulation ont échoué pendant
TLS avant l’envoi du POST (`sent=false` côté client), sans reçu d’acceptation :
elles ne constituent pas des exécutions supplémentaires.

Le passage en arrière-plan avant la première sortie, l’exécution de longue
durée, l’écran verrouillé et le GPU en arrière-plan restent non qualifiés.
Ces essais ne garantissent pas un serveur API permanent.

Les reçus publics sont conservés dans
`/Users/ales27pm/Library/Developer/Xcode/SwarmerAPIQualifications/20260920220700`,
séparément des informations privées de session API :

| Fichier | SHA-256 |
| --- | --- |
| `app-status-result.json` | `d9be30893b4c1a08b10aa8ee9ac73723f43914839592cd9f9541bd46442a2bc0` |
| `models-list-result.json` | `9f2e54c90c48f0b08834cc16372882b7fc5b0752f398c8f39e58b268b1d6a957` |
| `model-load-result.json` | `6184738e11fdbfbbb44310e32a0e68fc66c2402f63fd79f61236ac075993e232` |
| `warm-background-cancel-controlled-proof.json` | `dd98bbbb0e660c50f2803ba348f5ba8856f2424ba0e38926f3c9ecf0623186ff` |
| `warm-background-complete-controlled-proof.json` | `93b259bcb05133a4330bb733098cb7e64c7b0a19eec105614f328baa01589581` |
| `warm-background-complete-proof.json` (premier essai non qualifiant) | `a9e9abc0f0ea926450578ef0e0532d5adc51fbb724652760f60d14933c55cb11` |
