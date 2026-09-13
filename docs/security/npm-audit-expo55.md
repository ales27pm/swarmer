# Qualification npm / Expo SDK 55

Date du relevé: 2026-09-12. Le rapport complet `npm audit --json` de la
migration SDK 55 signale **18 entrées modérées, aucune haute ou critique**.
Les deux avis racines restent ouverts; ce résultat ne signifie pas que le
graphe est exempt de vulnérabilités.

## Périmètre et identité

- Rapport examiné: `swarmer-expo55-npm-audit-final.json`, conservé dans les artefacts
  locaux de qualification; SHA-256
  `02d2bdcaa4cf967fa33f8e2e8d8b38a4490a1eecd8b8d2b9eeaf60341860ca9f`.
- `mobile/package-lock.json` examiné: SHA-256
  `ad2828bce19829598e17b8c5b2616e7bef5c24cda8a1a127b743e4ffd8482669`.
- Versions principales: `expo@55.0.31`, `expo-router@55.0.18`,
  `jest-expo@55.0.22`.
- Ce rapport comprend les dépendances de développement. Il ne constitue pas
  un nouveau résultat `npm audit --omit=dev`, ni une analyse du contenu final
  de l'IPA. Les métadonnées npm de dépendances «prod» incluent aussi de
  l'outillage Expo exécuté lors du build.

Reproduction depuis `mobile/`, avec le lockfile qualifié:

```sh
npm audit --json
```

## Comparaison avec v0.11

Le [relevé du 8 septembre sous SDK 57](npm-audit-v011.md) conserve ses
résultats historiques: 13 entrées modérées. Les 18 entrées SDK 55 remontent
aux mêmes deux avis, et non à 18 vulnérabilités indépendantes.

| Avis racine | Entrées agrégées dans le rapport SDK 55 |
| --- | --- |
| `decode-uri-component` | `decode-uri-component`, `query-string`, `expo-router`, `@react-navigation/core`, `@react-navigation/native`, `@react-navigation/elements`, `@react-navigation/bottom-tabs`, `@react-navigation/native-stack` |
| `uuid` | `uuid`, `xcode`, `@expo/config-plugins`, `@expo/config`, `@expo/cli`, `@expo/prebuild-config`, `@expo/metro-config`, `@expo/local-build-cache-provider`, `expo`, `jest-expo` |

Les cinq entrées React Navigation et `jest-expo` s'ajoutent au relevé
historique, tandis que `@expo/inline-modules` n'y figure plus. Ce changement
de comptage décrit le graphe de dépendances; il ne mesure pas à lui seul
l'évolution de l'exploitabilité.

## Décodage d'URI: chemin de production à réévaluer

[GHSA-vcc3-ghjq-m6fr / CVE-2026-45822](https://github.com/advisories/GHSA-vcc3-ghjq-m6fr)
concerne les versions jusqu'à `0.4.2`; `0.5.0` corrige le décodage de certaines
entrées percent-encodées malformées pouvant saturer le CPU. Le lockfile
contient `decode-uri-component@0.2.2`, via `query-string@7.1.3`, lui-même
requis par Expo Router et React Navigation Core.

L'inspection des sources installées précise l'atteignabilité:

- `@react-navigation/core/src/getStateFromPath.tsx` appelle
  `queryString.parse(query)`. `query-string/index.js` active le décodage par
  défaut et appelle `decode-uri-component`. Ce chemin existe dans une
  dépendance de production.
- `expo-router/build/getLinkingConfig.js` fournit son propre
  `getStateFromPath`. Son fork `getStateFromPath-forks.js` lit les paramètres
  avec `URLSearchParams`; les appels `query-string` trouvés dans les forks de
  génération de chemins utilisent `stringify`. La simple présence du paquet
  ne prouve donc pas que les liens entrants de cette configuration atteignent
  la fonction vulnérable.
- Aucun appel direct à ces décodeurs n'a été trouvé dans `mobile/src`. Cette
  inspection statique n'établit ni une exploitation sur appareil, ni
  l'inaccessibilité de tous les chemins de navigation. L'avis reste ouvert,
  avec priorité à la validation des liens profonds et des routes web.

## UUID: chemin observé dans l'outillage de build

[GHSA-w5hq-g745-h8pq / CVE-2026-41907](https://github.com/advisories/GHSA-w5hq-g745-h8pq)
décrit des écritures partielles non rejetées lorsque les méthodes UUID
`v3`, `v5` ou `v6` reçoivent un buffer trop petit ou un offset invalide.
La branche antérieure à `11.1.1` est affectée. Le chemin verrouillé est
`@expo/config-plugins@55.0.11` → `xcode@3.0.1` → `uuid@7.0.3`.

Dans `xcode/lib/pbxProject.js`, le seul appel UUID trouvé est `uuid.v4()`
sans buffer fourni, pour générer les identifiants du projet Xcode. Le
déclencheur décrit par l'avis n'est pas présent dans cet appel observé.
Cette portée build réduit l'exposition constatée; elle ne justifie pas de
supprimer l'alerte ni d'affirmer que tout le graphe est sûr.

## Disposition

La migration SDK 55 répond à la compatibilité Xcode et n'est pas présentée
comme une correction de ces avis. Le graphe vient d'être résolu dans les
plages SDK 55; les versions corrigées des paquets racines dépassent les
plages déclarées `decode-uri-component: ^0.2.2` et `uuid: ^7.0.3`.

Le rapport propose notamment `expo@46.0.21`, `expo-router@5.1.11` et
`jest-expo@57.0.5`. Ces changements ne constituent pas une résolution
compatible et qualifiée du projet SDK 55. Certaines entrées transitives
portent aussi `fixAvailable: true`; ce champ seul ne prouve pas qu'une mise
à jour résoudra l'avis racine dans les contraintes actuelles.

Conserver les deux avis ouverts et réexaminer la résolution lorsqu'un
correctif compatible est disponible. Qualifier toute correction candidate
avec le diff du lockfile, un nouvel audit complet et `--omit=dev`, les
contrôles Expo, les tests de navigation puis le build natif. Un override
hors plage ou `npm audit fix --force` ne remplace pas cette qualification.
Cette note n'ajoute aucune mitigation et ne revendique aucune nouvelle
preuve de runtime; aucun paquet ni lockfile n'a été modifié pour l'écrire.
