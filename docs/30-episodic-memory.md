# 30 — Episodic Memory v0.12

## Statut

**IMPLEMENTED:** trajectoire terminale résumée, étapes expurgées, embedding
optionnel, recherche déterministe hybride/lexicale et projection du score de
feedback.

**EXPERIMENTAL:** qualité de ranking sur un historique réel et calibrage des
poids.

**PLANNED:** API/UI opérateur, index externe d'épisodes, déduplication
sémantique inter-buts, politique de rétention et effacement utilisateur.

La mémoire épisodique sert à récupérer des enseignements. Elle n'est ni une
source d'autorisation ni un journal brut complet.

## Écriture d'un épisode

Quand un but devient terminal, le Goal Manager rassemble uniquement des
résumés autoritatifs:

- objectif et taille du plan;
- outcome du but;
- durée observée;
- familles de workers utilisées;
- tags d'échec dérivés des états;
- une étape par nœud avec objectif/résultat ou erreur résumés;
- score de base observé (`1.0` pour completed, `0.0` sinon).

`EpisodeMemoryService.record_episode()` expurge et borne chaque texte avant
démarrage de la transaction. Il écrit atomiquement `episodes` et
`episode_steps`, avec un ordre de séquence stable. Une contrainte unique lie un
épisode à un `goal_run_id`.

Répéter exactement la même trajectoire retourne l'épisode existant. Tenter de
réécrire la sémantique d'un épisode déjà enregistré lève un conflit. Cette règle
empêche un callback tardif ou un retry de remplacer l'histoire terminale.

## Modèle de données

`episodes`:

- `id`, `goal_run_id`, `root_task_id`;
- `objective_summary`, `plan_summary`, `outcome`;
- `score`, `duration_ms`;
- `worker_types_json`, `failure_tags_json`;
- `user_feedback_score`;
- dates de création/mise à jour.

`episode_steps`:

- `id`, `episode_id`, `sequence`;
- type de nœud, skill, agent;
- résumés d'entrée/sortie;
- état terminal et latence optionnelle.

`episode_embeddings`:

- ID d'épisode et provider;
- dimensions et vecteur JSON;
- date de mise à jour.

SQLite demeure autoritatif. `episode_embeddings` est une projection
reconstruisible; FAISS v0.11 indexe seulement les `memory_embeddings`, pas les
épisodes.

## Embeddings et fallback

Après le commit de l'épisode, le service peut demander un vecteur à
`EmbeddingService`. Le vecteur est validé (dimensions, valeurs finies) puis
écrit par provider. Une panne ou réponse invalide du provider n'annule pas
l'épisode déjà enregistré.

Lors d'une recherche:

- si un embedding query et un embedding épisode compatibles existent, la
  pertinence utilise leur similarité et `search_kind` vaut `hybrid`;
- sinon la pertinence est lexicale et `search_kind` vaut `lexical`.

Le fallback lexical est toujours disponible et aucune perte d'index ne détruit
la trajectoire.

## Ranking

Jusqu'à 500 candidats récents sont lus avec un tie-break stable. Chaque score
final combine:

| Composante | Poids | Source |
|---|---:|---|
| pertinence sémantique ou lexicale | 0.45 | objectif, plan, outcome, skills, tags et étapes résumées |
| chevauchement de skills | 0.20 | skills demandés vs épisode |
| outcome préféré | 0.15 | préférence explicite de la requête |
| récence | 0.10 | date de l'épisode |
| score observé | 0.05 | résultat enregistré |
| feedback utilisateur | 0.05 | note 0–5 normalisée |

Sans score ou feedback, la composante reçoit une valeur neutre de `0.5`. Le tri
final est score décroissant puis ID d'épisode. Un agent ne peut pas s'auto-noter;
les valeurs proviennent du control plane et du feedback authentifié.

## Feedback

`POST /goals/{goal_id}/feedback` exige un but terminal. Après l'insertion
autoritative du feedback et de son audit, le service met à jour
`user_feedback_score` sur l'épisode correspondant. Cette seconde écriture est
une projection de retrieval: si elle échoue, le feedback reste accepté et peut
être reprojeté plus tard.

La note n'efface ni ne réécrit les étapes. Elle influence seulement les futures
recherches.

## Strategy hints

`StrategyRetrieval` utilise la recherche d'épisodes avec deux ensembles:

- outcomes de succès (`completed`, `success`, `succeeded`);
- outcomes d'échec (`failed`, `failure`, `budget_exhausted`, `dead_lettered`,
  `quarantined`).

Le hint de succès résume seulement l'objectif antérieur. Le hint d'échec résume
jusqu'à trois tags à éviter et l'objectif. Le service ne lit pas le résumé du
plan ni les étapes pour produire le texte du hint. Une trajectoire passée ne
devient donc pas un script à rejouer.

Les hints sont limités, expurgés et accompagnés d'IDs de provenance. Le planner
les reçoit comme conseils non fiables; le validateur et la Gateway restent
autoritaires.

## Confidentialité

- Les objectifs, plans et étapes sont expurgés avant persistance.
- Les identifiants de provenance sont conservés, pas les credentials.
- Un épisode ne contient aucun token de lease/grant, bearer ou résultat natif
  brut.
- Les exports d'entraînement repassent par le sanitizer de dataset.
- La politique de purge/rétention et le droit à l'effacement doivent encore
  être définis avant un usage contenant des données personnelles réelles.

## Invariants testés

- résumé/étapes secrets et chemins expurgés;
- écriture transactionnelle et ordre stable;
- idempotence exacte par but, conflit sur réécriture divergente;
- embedding optionnel validé;
- panne d'embedding sans perte de l'épisode;
- ranking sémantique, skill, outcome, récence et feedback;
- fallback lexical déterministe;
- hints de stratégie sans copie de plan.

## Limites

- Pas d'endpoint public de recherche/édition/suppression d'épisodes en v0.12.
- Pas de projection FAISS/Qdrant d'épisodes.
- Pas de preuve de qualité statistique du ranking sur un corpus de production.
- Les champs `episode_ids` du résultat mobile sont réservés mais non alimentés
  automatiquement par l'agrégateur actuel.
- Aucun épisode ne déclenche un auto-entraînement ou un changement de policy.
