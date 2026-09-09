# Qualification npm / Expo — préparation v0.11

Date du relevé: 2026-09-08. Cette note décrit le graphe verrouillé dans
`mobile/package-lock.json`; elle n'est ni une preuve qu'une vulnérabilité est
exploitable, ni une autorisation d'ignorer une alerte.

## Résultat de référence

Les deux commandes suivantes, exécutées avec Node 24 et npm sans modifier le
lockfile, retournent le même résultat:

```text
npm audit --json
npm audit --omit=dev --json

moderate: 13
high: 0
critical: 0
total: 13
```

`npm audit` agrège les dépendances affectées. Les 13 entrées ne représentent
pas 13 avis indépendants: elles remontent à deux avis racines transitifs.

| Entrée agrégée | Niveau npm | Avis racine / chemin observé | Disposition v0.11 |
|---|---:|---|---|
| `@expo/cli` | moderate | Transitif via Expo config, Metro et prebuild | Suivre la correction Expo compatible SDK 57 |
| `@expo/config` | moderate | Transitif via `@expo/config-plugins` | Même disposition |
| `@expo/config-plugins` | moderate | `xcode` → `uuid` | Même disposition |
| `@expo/inline-modules` | moderate | Transitif via `@expo/config-plugins` | Même disposition |
| `@expo/local-build-cache-provider` | moderate | Transitif via `@expo/config` | Même disposition |
| `@expo/metro-config` | moderate | Transitif via `@expo/config` | Même disposition |
| `@expo/prebuild-config` | moderate | Transitif via Expo config/plugins | Même disposition |
| `decode-uri-component` | moderate | GHSA-vcc3-ghjq-m6fr | Priorité runtime; attendre une résolution Expo Router compatible |
| `expo` | moderate | Agrégation des dépendances Expo CLI/config | Ne pas rétrograder Expo |
| `expo-router` | moderate | `query-string` → `decode-uri-component` | Priorité runtime; tester la correction officielle |
| `query-string` | moderate | `decode-uri-component` | Même disposition |
| `uuid` | moderate | GHSA-w5hq-g745-h8pq | Portée build observée; suivre le correctif Expo/xcode |
| `xcode` | moderate | `uuid` | Même disposition |

## Avis racines et atteignabilité

### Décodage d'URI

- Version verrouillée: `decode-uri-component@0.2.2`.
- Chemin: `expo-router@57.0.20` → `query-string@7.1.3` →
  `decode-uri-component@0.2.2`.
- Avis: GHSA-vcc3-ghjq-m6fr, déni de service par décodage exponentiel d'une
  entrée percent-encodée malformée.
- Atteignabilité: `expo-router` importe `query-string` pour transformer les
  chemins et paramètres. Une URL ou un lien profond hostile est donc une
  entrée potentielle; l'avis n'est pas classé «build only».
- État amont observé: `decode-uri-component@0.5.0` est hors de la plage
  `^0.2.2`; `query-string@9.5.1` utilise `^0.5.0`. Forcer ces versions à travers
  leurs plages 0.x/majeures sans validation Expo pourrait casser le routeur.

### UUID du parseur Xcode

- Version verrouillée: `uuid@7.0.3`.
- Chemin: Expo config plugins → `xcode@3.0.1` → `uuid@7.0.3`.
- Avis: GHSA-w5hq-g745-h8pq, vérification de bornes absente pour UUID
  v3/v5/v6 lorsqu'un buffer est fourni.
- Atteignabilité observée: le code verrouillé de `xcode` appelle
  `uuid.v4()` pour produire les identifiants de projet. Aucun appel v3/v5/v6
  n'a été trouvé dans ce paquet. Cela réduit l'atteignabilité constatée et
  place ce chemin dans l'outillage de génération native; ce n'est pas une
  preuve générale d'absence de risque dans tout le graphe.
- `uuid@11.1.1` ou ultérieur est hors de la plage déclarée `^7.0.3`; un override
  majeur n'est pas accepté sans qualification de l'outillage Expo.

## Compatibilité Expo observée

Au même commit:

```text
npx --no-install expo-doctor
21/21 checks passed. No issues detected.

npx --no-install expo install --check
Dependencies are up to date
```

L'application est donc cohérente avec la matrice Expo SDK 57 actuellement
installée. Ce résultat ne remplace ni un prebuild propre, ni une compilation
iOS/Android, ni un essai sur appareil.

## Décision de traitement

`npm audit` propose notamment `expo@46.0.21` et `expo-router@5.1.11` comme
«fix». Il s'agit de rétrogradations majeures incompatibles avec l'application
SDK 57. Par conséquent:

1. ne pas exécuter `npm audit fix --force`;
2. ne pas ajouter d'override transgressant les plages de `query-string`,
   `decode-uri-component`, `xcode` ou `uuid` sur la branche principale;
3. surveiller une publication Expo SDK 57 qui mette à jour ces dépendances;
4. qualifier toute résolution candidate sur une branche isolée avec le diff du
   lockfile, `npm audit`, `expo install --check`, Expo Doctor, typecheck, lint,
   Jest, puis un prebuild/compile natif propre;
5. réévaluer en priorité le chemin URL/lien profond, qui est le chemin runtime
   potentiellement atteignable;
6. conserver ce relevé comme exception temporaire datée, et le remplacer par
   un nouveau relevé après toute mise à jour Expo.

Aucune version de paquet ni aucun lockfile n'a été modifié pour produire cette
qualification.
