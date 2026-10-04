# ANEMLL : intégration du pipeline et limites de qualification

L'adaptateur cible uniquement [`anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0`](https://huggingface.co/anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0/tree/c6461a77a6f803424ec347f9537aadac37094879), révision `c6461a77a6f803424ec347f9537aadac37094879`. Il complète les [chargements indépendants sur iPhone](anemll-reference-load-2026-10-04.md) ; ces chargements seuls ne prouvent pas une génération.

## Contrat implémenté

- Trois composants compilés : embeddings, tête de langage et FFN multifonction `prefill`/`infer`. Le profil de téléchargement exige les quinze membres compilés attendus et les trois fichiers config/tokenizer, avec tailles et empreintes publiques. Les dispositions incomplètes, mélangées ou non épinglées sont refusées.
- Le stockage conserve l'origine Hugging Face sans nouvelle version de son schéma. Le chargeur mono-modèle existant reste inchangé ; le nouvel acteur est sélectionné pour le dossier ANEMLL validé, puis contrôle les contrats Core ML réels.
- Le runtime charge avec CPU + Neural Engine par défaut, sans repli automatique GPU. Chaque génération crée son état frais, partagé séquentiellement entre les fonctions de préremplissage et de décodage.
- Contexte compilé de **512 tokens**, préremplissage par lots de **64**, padding `128004`. Aucun texte du prompt n'est tronqué. Le budget de sortie demandé est plafonné à l'espace restant ; les réglages utilisateur ne sont pas réécrits.
- Après préremplissage, le dernier token réel est inféré à sa position `N−1` : la sortie du préremplissage n'est pas une sortie normalisée utilisable directement par la tête. Le masque laisse voir seulement `j ≤ p`, sans exposer le slot de padding suivant. Les huit partitions de logits sont concaténées dans l'ordre numérique.
- Annulation/déchargement invalident l'opération sans libérer son verrou avant le retour de l'appel Core ML déjà engagé. Une nouvelle génération ne reprend jamais l'ancien cache.

Référence de comparaison : code ANEMLL 0.3.0 au commit `921489c9b241d678c3ca2cd03eef5b1c56d00572`, métadonnées et graphes MIL des fichiers épinglés. L'exemple Swift amont laisse visible un slot futur au décodage ; l'adaptateur applique le masque causal à la position réellement écrite.

## Vérifications locales

- 108 tests TypeScript du résolveur et du composant de téléchargement ; TypeScript et ESLint passent.
- 14 tests natifs de téléchargement/import, dont la conservation de l'origine après redémarrage et les refus de profils invalides.
- 20 tests du stockage local ; 7 tests de l'ancien import diagnostique Core ML.
- Helpers ANEMLL exécutés : frontières de lots 1/63/64/65/511, padding, masque, limite de sortie, vocabulaire, échantillonnage et verrou d'annulation.
- Typecheck Swift 6 strict Debug et Release iPhone arm64 avec les vrais modules Core ML/Tokenizers ; seuls les types d'erreur/résultat de l'app sont substitués pour ce contrôle isolé. Ce n'est pas une compilation complète de l'app.
- Résolution HTTP réelle depuis le Mac : un choix, 18 fichiers, **1 073 756 426 octets**. Cette lecture de métadonnées ne télécharge pas les poids sur l'iPhone.
- 99 tests du registre API passent. `models.huggingface.download` reçoit le runtime et un lien public, résout ses métadonnées puis exige un choix unique ou un identifiant de choix exact. Aucun manifeste fourni par le client n'est accepté. Le téléchargement UI et celui de l'API partagent un verrou ; leurs annulations sont liées au propriétaire.

Revue indépendante des raccordements, du moteur et des helpers : aucun défaut concret retenu. Les fixtures de téléchargement natives contiennent de petits fichiers factices et testent le transport/stockage, pas les graphes Core ML.

- 26 tests du pont UI et du protocole passent avec le vrai verrou partagé ; lint ciblé vert. Les anciennes attentes du pont UI ont été mises à jour pour annuler pendant une opération réellement active.

## Premier passage réel dans monGARS

La révision `873e662` a été compilée en Debug pour iPhone en **128,90 s**, signée, puis installée par IPA complète sur l'iPhone 16 Pro (`iPhone17,1`, iOS 26.7 bêta). Les fixtures de diagnostic sont inchangées. Une première installation a rencontré une coupure CoreDevice ; la seconde a réussi, sans désinstallation ni suppression des modèles existants.

L'appel `models.huggingface.download` a téléchargé et importé les **18 fichiers / 1 073 756 426 octets en 105,45 s**, avec la provenance épinglée. Le chargement demandé ensuite en `cpuAndNeuralEngine` s'est arrêté à **`validateArtifact` en 1,26 ms**, avant les appels Core ML. Le reçu initial incertain a été résolu par une lecture d'état explicite : moteur `failed`, aucune génération lancée. Ce n'est pas une reproduction de l'erreur Core ML −14.

La comparaison brute des chemins du profil ANEMLL a été identifiée comme cause : le stockage résout un chemin sous `/var`, tandis que Foundation peut énumérer les mêmes fichiers sous `/private/var`. La régression échoue après `resolve` avant correction (13/14), puis les mêmes 14 tests passent avec revalidation après réouverture. Les refus de symlinks, bornes et ensembles de fichiers restent inchangés.

La révision `6bea3ce` a été recompilée en **112,36 s** et installée avec succès. Sur le même modèle déjà importé, les quatre chargements Core ML passent en `cpuAndNeuralEngine`, puis la validation du contrat échoue après **43,89 s**, avant tokenizer et génération. Cela confirme la correction du chemin et situe le défaut suivant dans les attentes du nouvel adaptateur, sans reproduire −14. Les reçus distincts sont conservés dans `anemll-integration-20261004/path-fix`.

Le contrôle des contrats a ensuite été confronté aux descriptions **Core ML réelles** sur Mac : chargement CPU des embeddings, de la tête et de `infer`, description publique `MLModelAsset` pour `prefill`. Les tenseurs fixes sont exposés comme des ensembles de **forme unique** (`enumerated`), tandis que seule la sortie dynamique des embeddings est `unspecified`. Le validateur exigeait ce dernier type partout : cette interprétation des métadonnées était incorrecte. Le correctif exige maintenant le singleton exact pour les formes fixes, conserve le cas vide dynamique, et refuse plages, formes alternatives et dimensions différentes. Les dumps se trouvent dans `anemll-integration-20261004/contract-probe` ; ils ne constituent pas une mesure de prédiction ni d'activité ANE.

Les reçus privés se trouvent dans `Library/Logs/SwarmerQualification/CoreML/anemll-native-runtime-20261004/checks` et `anemll-integration-20261004`. Le chemin de génération est confirmé dans les essais ci-dessous ; sa qualité et son équivalence à la référence restent à qualifier. Même un succès en configuration CPU + Neural Engine ne mesure pas à lui seul l'activité matérielle du Neural Engine.


## Qualification iPhone après les deux corrections

La révision `c4eebc5` est compilée en **117,84 s**, signée et installée par IPA complète. La session privée iPhone confirme un chargement complet en **44,10 s**, `cpuAndNeuralEngine`, sans erreur. Le tokenizer et les contrats réels sont acceptés. Les poids déjà importés sont conservés.

| Demande réelle, température 0 | Résultat | Durée acceptation→fin API |
| --- | --- | ---: |
| « Réponds uniquement : Modèle prêt. » | Réponse différente de la phrase requise, 16 tokens, arrêt normal | 1,281 s |
| Deux plus trois, nombre seul, en français | `27`, réponse incorrecte, 1 token | 0,319 s |
| Répétition exacte du premier prompt après le calcul | Explication de 337 tokens, arrêt normal ; résultat différent | 19,073 s |
| Même calcul en anglais | `5`, correct, 1 token | 0,305 s |

Ces durées incluent le traitement API, pas uniquement l'inférence. Chaque appel demande un plafond de 1 024 tokens ; le runtime applique l'espace réellement disponible dans le contexte compilé de 512 tokens sans modifier les réglages enregistrés.

**Statut expérimental.** Téléchargement épinglé, import, chargement et génération sont démontrés dans monGARS sur l'iPhone. Cela ne qualifie ni l'exactitude en français, ni l'isolation du cache entre requêtes, ni la parité avec l'application ANEMLL de référence. La variation du prompt répété est observée ; elle ne prouve pas à elle seule une fuite de cache ou un défaut des poids. Les appels `MLTensor` diffèrent du chemin `MLFeatureProvider` de référence : une comparaison contrôlée des mêmes tokens, états et logits est la prochaine vérification, avant une modification spéculative. Apple documente les [opérations de tenseur asynchrones](https://developer.apple.com/videos/play/wwdc2024/10161/) et la [sérialisation de l'état](https://developer.apple.com/documentation/coreml/mlstate) ; ces règles seules ne démontrent pas la cause de cet écart.

Reçu privé : `Library/Logs/SwarmerQualification/CoreML/anemll-integration-20261004/contract-fix/qualification-receipt.json`. Aucun benchmark de qualité global ni mesure d'activité matérielle ANE n'a été réalisé. Le backend n'a pas été déployé dans ce lot.
