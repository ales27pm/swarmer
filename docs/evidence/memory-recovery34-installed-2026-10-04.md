# Recovery34: qualification locale entre paquets installés

Date : 4 octobre 2026. Le candidat exact
`3358210cb50a33521bac4a2a8f2b11592434d05a` et recovery34 ont été construits,
installés et vérifiés dans deux venv privés macOS. Les trois parcours
**candidat installé → recovery installée → candidat installé** ont réussi.
Ces paquets n'ont pas été déployés. Aucun service, appareil, modèle, fournisseur
ou stockage de production n'a été sollicité par cette qualification.

Cette preuve complète la compatibilité locale requise par la tranche
[leçons conditionnelles](memory-conditional-lessons-2026-10-04.md). Elle ne clôt
ni M09 ni le plan produit complet. La
[qualification recovery33](memory-recovery33-qualification-2026-10-04.md)
reste une preuve historique distincte et inchangée.

## Paquets et provenance

Racine privée `M` :

```text
/Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/m09-receipt-lessons-20261003
```

- `R = M/recovery34-20261004` : copie minimale de la recovery33 scellée,
  schéma d'écriture historique 29, lecture maximale 34. Aucun service ou route
  de leçons, nouvelle acceptation M09, migration vers 34 ou promotion ajouté.
- `I = M/issuer-installed335-20261004` : archive Git exacte du commit 3358210,
  schéma 34. La construction réutilise les outils hors réseau et les dépendances
  de recovery33 ; elle ne reprend pas un checkout de travail comme source.
- Recovery : 1 011 fichiers source, **134 fichiers applicatifs**.
  Candidat : 1 110 fichiers source, **149 fichiers applicatifs**.
  Pour chaque paquet, les fichiers applicatifs source, archive, wheel et installés
  correspondent exactement. Les vérifications d'origine refusent les imports
  applicatifs hors du venv désigné.
- Les deux `pip check` passent. Les arbres de dépendances ont été comparés à leurs
  sceaux de construction après l'aller-retour ; ils sont inchangés. Les 72 versions
  de distributions de l'environnement recovery sont conservées.

| Élément | SHA256 |
| --- | --- |
| Sceau de construction R34 | `ae7c7c88f731123d6415c5824c71d99e4afd04df4963459998c12b2f9ff77c98` |
| Provenance R34 | `a5a5b15f2b5293f9c8b626aaea31c532280054e45083cbb1c95ac6fa7fef495f` |
| Archive source R34 | `e974793c04f5a2b73dd69f0cae34f216d6494d7335475a57a87cf1cc27374bb4` |
| Wheel R34 | `fbe45ceecf582817b2bf7b15da0427631dd7ec472e704fa1da7af60129d9842f` |
| Descripteur runtime R34 | `4635d73fb7933e027132b0bea105a3751281ccda1d69dfa2d4b7e2244923e9c3` |
| Sceau de construction candidat | `6233a1b97e3f9951e1c994bcb043f378560a97ba5036988fbc0e9d7fee04effc` |
| Provenance candidat | `a241d954fc5990b7b113ede8fab401cae242a4504ab6c0b07dda4ddd7dcbed9f` |
| Archive source candidat | `c7a56dca908a89dc66b4cc2b619bd9fd6d18e284a7aeb70a26cf9d641db17cd8` |
| Wheel candidat | `fa114e39d9e474f42ed6e31ecd61a01b8647c2fc32ff93cd7a69ca1b1c677279` |
| Descripteur runtime candidat | `0ae80765a372bde1e5636ede849e3088a46baa53d257e66d4b522eda67d7e8e4` |

## Résultats vérifiés

Recovery33 refuse les deux nouvelles bases peuplées de schéma34 : ce résultat
rouge est conservé. La copie recovery34 passe ensuite **50 contrôles source**
(7,63 s), puis les **50 contrôles sur recovery installée** (6,22 s).
Les 29 contrôles hérités sont exécutés à nouveau, avec 21 contrôles nouveaux.
Les 7 contrôles du builder et les 6 contrôles du harnais d'aller-retour passent.
Ruff et les vérifications de types ciblées de recovery passent également.

Les contrôles couvrent notamment le refus des schémas futurs, incomplets ou
altérés avant récupération/DML ; les préfixes29–33 ; les données peuplées34 ;
les effacements transactionnels avec FK activées ou désactivées ; les limites de
projet ; le rejeu terminal ; et le maintien des guards des workers historiques.
Un défaut d'effacement d'une preuve orpheline d'une ancienne version a été
reproduit, puis corrigé dans la copie privée. Son test rouge est conservé.

Le harnais final crée une fixture fraîche via le **candidat installé**, en
employant les helpers synthétiques du commit exact. Il importe et vérifie le
paquet `app` installé avant de rendre ces helpers accessibles. Tous les modules
`app.*` restent dans le venv prévu ; les helpers restent dans les tests exportés
du commit. Les processus de retour n'exposent aucun chemin source applicatif.

| Parcours | Mutations permises dans recovery | Résultat après retour au candidat installé |
| --- | --- | --- |
| Conservation | Aucune table modifiée | Les deux rejeux retrouvent la version2 et l'observation déclarée `passed`, sans nouvelle écriture. |
| Effacement des leçons du projet | Seulement `memory_procedure_lessons` et `memory_procedure_evidence` | Les anciennes clés de proposition et d'évaluation retournent `lesson_not_found`/404. Aucune leçon n'est recréée. |
| Effacement partiel des reçus | Seulement `project_execution_acceptances` et `project_execution_revision_links` | La sélection de preuves des leçons reste présente ; les deux rejeux retournent une observation `unknown`, sans nouvelle écriture. |

Chaque phase démarre le runtime deux fois. Les instantanés comprennent le schéma,
`user_version`, toutes les tables, toutes les colonnes, les rowids, les valeurs
BLOB et toutes les lignes de `sqlite_sequence`. Le retour au candidat conserve
exactement l'état laissé par recovery. L'intégrité SQLite, les FK et le parsing
historique du reçu `ProjectResult` sont vérifiés. Les phases recovery chargent
54–55 modules applicatifs installés ; les phases de retour en chargent 66.
Les inventaires, archives, wheels, descripteurs et origines sont revérifiés.

L'effacement des leçons conserve les tombstones de requête dont le `scope` est
le `project_id` : scope, request_id, empreinte, target_id et date. Elles ne
contiennent ni texte de leçon ni contenu de preuve. Les profils globaux et leurs
requêtes sont conservés. Cet effacement est distinct de celui des reçus et ne
constitue pas un effacement global : les JSON historiques de jobs et de révisions,
les mémoires et les autres artefacts ont leurs propres responsabilités d'effacement.

## Reçus conservés

| Reçu privé | SHA256 |
| --- | --- |
| `R/checks/installed-tests-01/receipt.json` | `381fc2777ae8237ff184aa94a2b84fd825bad0159925885061cc3d1dfe9dc637` |
| `R/checks/installed-final.json` — étape antérieure avec retour candidat source | `2df63021680e4ef9ca33150a9d28c75e613f036153f89f7d5d9e5193647aec03` |
| `I/roundtrip-plan.json` | `73b507c0f58c8ce632e084f4a6856abc7ff5f47d6b7ee7fc3d53dc1e14d3b26e` |
| `I/roundtrip-01/receipt.json` — trois parcours entièrement installés | `596d2037600facba2ce769dba917707b0f208d66ebd3bf0c5ce5a0378363556f` |
| `I/artifact-manifest.json` | `af36c4a236ad688068d5c49aaa4c80b5a04b5f33d94de0bd816765f966f0d197` |
| `I/final-receipt.json` | `e7f4e498e5ba34109b2282197aa83761a2c0383be30acb36a4e847cb09faaa8c` |

Le reçu antérieur avec candidat source n'est pas réécrit ni présenté comme une
preuve entre deux paquets installés. Les échecs de développement et leurs logs
restent également conservés dans le dossier privé.

## Reproduction privée

Cette commande utilise les deux paquets déjà construits et leurs pins. Choisir
un **nouveau** nom de dossier directement sous `I` ; le harnais refuse une sortie
existante. Elle crée exclusivement des bases synthétiques locales.

```sh
I=/Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/m09-receipt-lessons-20261003/issuer-installed335-20261004
sudo -n -H -u ales27pm /Users/ales27pm/swarmer/server/.venv/bin/python -I -B \
  "$I/installed_roundtrip.py" \
  --output "$I/roundtrip-reproduction-01" \
  --issuer-provenance-sha256 a241d954fc5990b7b113ede8fab401cae242a4504ab6c0b07dda4ddd7dcbed9f \
  --recovery-provenance-sha256 a5a5b15f2b5293f9c8b626aaea31c532280054e45083cbb1c95ac6fa7fef495f
```

Harnais figé :
`4806edefdc973ea2c64ccb300e886062ce9e32309148739d2c79443e90511556`.
Les processus Python interdisent `socket.connect`, `socket.getaddrinfo` et
`socket.sendto` par audit. Il ne s'agit pas d'une isolation réseau au niveau OS.

## Limites

Les rapports d'exécution et approbations de profil des fixtures sont
**synthétiques**. Aucun vrai worker ou oracle de production n'a été exécuté.
`passed` décrit ici l'observation du rapport ; l'origine reste
`worker_reported_measurement`, l'assurance `authenticated_lease_only`,
`grants_authority=false` et `promotion=none`. Aucune attestation, promotion,
normalisation assouplie ou modification des contrats de retries/idempotence
n'est introduite par ces paquets.

Cette preuve porte sur des paquets et venv privés macOS. Elle ne prouve pas une
construction Linux, un déploiement Ubuntu, une activation de cohorte, un usage
iPhone ou un profil d'exécution de production qualifié. Les étapes d'admission,
de staging, de recovery et de vérification live restent distinctes.
