# 31 — Swarm Evaluation v0.12

## Statut

**IMPLEMENTED:** contrats d'évaluation stricts, scénarios déterministes du
runtime de buts, exports JSONL par rôle et garde explicite des candidats de
fine-tuning.

**QUALIFIED:** invariants couverts dans le harness automatisé local. La preuve
Redis/multi-worker reste celle de v0.11 et la validation iPhone physique reste
séparée.

**EXPERIMENTAL:** qualité réelle des modèles planner/evaluator, endurance et
mesures statistiques sur un corpus représentatif.

**PLANNED:** runner d'evals versionné, splits, comparaisons avant/après,
promotion/rollback de modèles et revue de lots.

## Deux significations d'«evaluation»

Le runtime distingue:

1. **Evaluator online:** un modèle propose la prochaine décision d'un but à
   partir d'un état borné. Cette sortie ne devient autoritative qu'après les
   contrôles du Goal Manager.
2. **Evaluation offline:** les trajectoires et corrections sont exportées pour
   tester ou entraîner ultérieurement des modèles. Cet export n'agit jamais sur
   le runtime live.

Confondre les deux créerait une boucle d'auto-validation. Aucun résultat modèle
n'est une preuve de réussite, et aucun score exporté ne modifie une policy.

## Contrat evaluator online

`EvaluationDecision` version `1.0` est strict:

```json
{
  "schema_version": "1.0",
  "status": "continue",
  "reason_summary": "…",
  "missing_requirements": [],
  "invalid_results": [],
  "suggested_new_nodes": [],
  "user_question": null,
  "completion_summary": null
}
```

Règles:

- `needs_user` exige une question et les autres états la refusent;
- `done`, `failed` et `needs_user` ne peuvent pas suggérer de nœuds;
- les nœuds de `continue`/`replan` suivent le même contrat que le planner;
- les dépendances doivent viser un nœud connu ou une suggestion valide;
- les skills sont réévalués par la policy serveur;
- un champ inconnu ou de succès dans un nœud est refusé;
- un fingerprint couvre la décision de contrôle, pas seulement sa prose.

Le provider Ubuntu utilise un endpoint OpenAI-compatible avec température zéro
et `response_format` JSON Schema strict. Un provider noop et un provider fixe
existent seulement pour les tests. Le `ModelRouter` immuable choisit les IDs du
planner et de l'evaluator et expose des métadonnées sûres par rôle, mais ne
possède aucune fonction d'exécution.

## Oracle autoritatif

Le Goal Manager ne délègue jamais ces décisions au modèle:

- état courant et transition possible;
- présence d'une preuve worker complétée;
- skill/payload autorisé;
- budgets restants;
- lease/génération et identité de l'agent;
- accord Gateway/capability;
- résultat terminal et annulation;
- détection de boucle.

Une décision `done` sans preuve worker est rejetée. Une réponse malformed ou un
transport indisponible laisse un état récupérable ou termine selon le budget;
elle ne simule pas de succès.

## Scénarios automatisés v0.12

`server/tests/test_swarm_eval_scenarios.py` couvre:

| Scénario | Invariant attendu |
|---|---|
| analyse de dépôt parallèle | nœuds indépendants dispatchés dans la limite |
| recherche + fichiers | payloads et sorties bornés |
| échec worker | limitations conservées, evaluator non autoritaire |
| `needs_user` | but en attente sans action implicite |
| ancienne lease après réaffectation | résultat stale refusé |
| replan équivalent | boucle arrêtée |
| annulation utilisateur | completion tardive fencée |
| budget d'appels modèle | arrêt avant appel supplémentaire |
| épisode similaire | hints bornés transmis au planner |
| preuves contradictoires | résolution utilisateur exigée |

Les tests de services ajoutent:

- parsing strict du plan/evaluator, DAG acyclique et fingerprints;
- dépendances hard/optional et non-résurrection de nœud;
- profils manuels et parallélisme;
- résultat agrégé expurgé et provenance;
- contextes déterministes avec budgets indépendants;
- épisodes idempotents, recherche hybride/fallback lexical;
- exports de trajectoires sans secrets et filtres indépendants.

Ces tests qualifient la logique dans un environnement contrôlé. Ils ne mesurent
pas la qualité d'un modèle réel, la saturation GPU, une panne d'hôte physique ou
un effet iOS.

## Datasets offline

`FeedbackDatasetService.export_goal_jsonl()` fournit quatre vues:

### Planner

Objectif, critères, DAG validé, états finaux et métadonnées d'appels planner.
Utile pour vérifier décomposition, dépendances, skills et budgets.
Chaque nœud exporte `dependencies` et `optional_dependencies` depuis les arêtes
SQLite autoritatives, dans un ordre stable, y compris pour un DAG dense.

### Evaluator

Décisions séquencées, fingerprints, outcomes de nœuds et métadonnées d'appels
evaluator. Utile pour tester `continue/replan/done/failed/needs_user`.

### Synthesis

Outcomes de nœuds, résultat agrégé sûr et métadonnées d'appels de synthèse
disponibles. La synthèse live v0.12 est déterministe; l'existence de ce format
ne prouve pas un synthétiseur LLM branché.

### Routing

Affectations, skills et décisions/scoring expurgés du scheduler. Aucune
credential, endpoint privé ou payload sensible.

Tous les records portent provenance et outcome, repassent par le sanitizer et
sont bornés en taille. Les filtres permettent d'inclure échecs ou feedback non
revus pour une eval diagnostique sans les transformer en cible.

## Garde de fine-tuning

Un record devient `fine_tune_candidate` seulement avec:

- but `completed`;
- feedback `reviewed`;
- score au moins 4/5;
- correction humaine non vide adaptée au rôle.

Le flag est une présélection, pas une approbation d'entraînement. Avant tout
LoRA, il reste nécessaire de:

1. versionner le dataset et sa requête d'export;
2. faire une revue PII/secrets/licence;
3. dédupliquer et séparer train/validation/test;
4. définir des métriques et un baseline;
5. exécuter les régressions Permission Gateway et schémas;
6. signer une promotion explicite et conserver un rollback.

Aucune de ces étapes n'est automatisée en v0.12.

## Matrice de qualification

| Niveau | v0.12 | Ne prouve pas |
|---|---|---|
| UNIT | contrats, états, budgets, contexte, épisodes, datasets | modèle réel ou infrastructure |
| INTEGRATION | API/worker hooks dans les tests; fabric v0.11 séparé | deux machines physiques |
| CHAOS | invariants v0.11 conservés; scénarios goal bornés | panne réelle datacenter/GPU |
| MODEL QUALITY | **EXPERIMENTAL / NOT QUALIFIED** | précision générale |
| PHYSICAL IPHONE | **MANUAL VALIDATION REQUIRED / NOT RUN** | capacités iOS réelles |

Les chiffres exacts de la release doivent provenir du gate intégré final, pas de
ce document ni d'un sous-ensemble de tests.

## Critères avant une revendication production v0.12

- gate complet reproductible avec nombres de tests consignés;
- modèle planner/evaluator réellement chargé et scénarios représentatifs;
- tests de charge et endurance sur le matériel Ubuntu visé;
- backup/restore SQLite et alerting opérationnels;
- topologie workers physiques qualifiée si elle est revendiquée;
- Redis TLS/PKI qualifié si Redis est utilisé en production;
- matrice iPhone physique exécutée pour toute capability revendiquée;
- revue des datasets avant tout entraînement.

Jusqu'à ces preuves, la formulation sûre est: «runtime autonome borné et testé
localement», pas «swarm autonome production-ready».
