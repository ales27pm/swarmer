# 29 — Context Engineering v0.12

## Statut

**IMPLEMENTED:** construction déterministe, bornée et expurgée des contextes de
but, provenance persistée et retrieval de hints de stratégie.

**EXPERIMENTAL:** mesure réelle des tokens selon chaque tokenizer et qualité du
retrieval sur des corpus longs.

**PLANNED:** sélection vectorielle externe des épisodes et policy de rétention
des contextes.

Un contexte aide un modèle à proposer. Il ne porte aucune permission, preuve de
lease, credential ou autorité de transition.

## Sources autoritatives

`ContextBuilder` accepte seulement `goal_run_id`, `node_id` optionnel et un
purpose sûr. Il ouvre une transaction de lecture cohérente et charge depuis
SQLite:

1. le but et ses critères;
2. la tâche racine;
3. le nœud courant;
4. les contraintes de policy/autonomie;
5. les budgets et compteurs;
6. les échecs du but et des nœuds;
7. les dépendances upstream terminées;
8. des épisodes historiques;
9. des mémoires;
10. des cartes d'agents validées et leurs scores observés.

Le modèle ne peut pas injecter une source arbitraire dans ce pack. Les cartes
d'agents sont des métadonnées approuvées par le serveur, pas des manifests
worker bruts.

## Budgets durs

Valeurs par défaut:

| Setting | Défaut | Effet |
|---|---:|---|
| `MONGARS_GOAL_CONTEXT_MAX_TOKENS` | 2048 | somme approximative maximale des cartes retenues |
| `MONGARS_GOAL_CONTEXT_MAX_MEMORY_ITEMS` | 6 | mémoires candidates maximales |
| `MONGARS_GOAL_CONTEXT_MAX_EPISODE_ITEMS` | 4 | épisodes candidats maximaux |
| `MONGARS_GOAL_CONTEXT_MAX_AGENT_CARDS` | 6 | cartes d'agents maximales |
| `MONGARS_GOAL_CONTEXT_MAX_UPSTREAM_RESULTS` | 8 | résultats upstream maximaux |
| `MONGARS_GOAL_CONTEXT_MAX_RESULT_CHARS_PER_NODE` | 2000 | caractères maximum de chaque résultat/erreur de nœud avant assemblage |

Les limites par source sont appliquées dans les requêtes SQL avec un ordre
stable. Elles restent vraies même si le budget de tokens permet davantage
d'items. Le budget de tokens peut retenir moins d'items, jamais plus.

Le compte de tokens est une approximation déterministe `ceil(octets UTF-8/4)`
sur le payload JSON canonique réellement transmis; ce n'est pas le tokenizer
propre à un modèle. Le
builder réserve la taille minimale des cartes prioritaires, tronque une carte
par recherche binaire puis arrête avant la limite. À état identique, les cartes,
leur ordre, leur troncature, leur provenance et le compte sont identiques.

Deux paramètres historiques, `max_items` et `max_upstream_chars`, restent
acceptés comme caps additionnels pour compatibilité interne. Le runtime v0.12
utilise les six limites explicites ci-dessus.

## Ordre et priorité

Les cartes non-agent sont proposées dans cet ordre:

```text
goal
root_task
current node
constraints
budgets
goal/node failures
completed upstream results
prior episodes
memory items
```

Les cartes d'agents sont ajoutées ensuite si le budget restant le permet. Dans
chaque catégorie, les tie-breaks finissent par un ID stable. La provenance est
calculée seulement depuis les cartes réellement retenues; une source tronquée
hors du pack n'est pas annoncée.

## Expurgation avant persistance

Toute chaîne passe par le sanitizer de dataset puis par des filtres de contexte
pour:

- bearer/basic auth et secrets `key=value`;
- tokens communs, clés privées, credentials URL et formes de JWT;
- chemins absolus Unix/macOS/Windows et racines protégées;
- contrôles et whitespace non sûrs.

L'expurgation précède la troncature, afin qu'un préfixe de secret ne survive pas
à une coupure. Seuls les `ContextCard` déjà expurgés sont sérialisés dans
`goal_contexts`. Le prompt racine brut, les payloads worker et les arguments de
capability ne sont pas persistés dans cette table.

Une carte contient:

```json
{
  "card_id": "upstream:node_01",
  "kind": "upstream",
  "summary": "Completed upstream …",
  "provenance_ids": ["node_01"]
}
```

La ligne de contexte ajoute `goal_run_id`, tâche racine, nœud éventuel, purpose,
liste stable de `card_ids`, liste stable de sources, compte de tokens et date.

## Contextes planner et evaluator

### Planner — IMPLEMENTED

Le Goal Manager transforme objectif, critères, budgets, guidance de replan et
`strategy_hints` en cartes avant la sélection. Il transmet exactement le JSON
canonique expurgé et borné persisté dans `goal_contexts`; son digest et son
compte couvrent donc tout le contenu réellement envoyé. Le provider borne le
transport à 256 KiB et demande une réponse JSON Schema stricte.

Le planner n'obtient aucun executor. Après sa réponse, le serveur revalide le
DAG, les skills et les limites avant toute écriture.

### Evaluator — IMPLEMENTED

Le builder produit un `GoalEvaluationContext` Pydantic expurgé et borné:
objectif, critères, résumés de nœuds, budgets restants, temps et fingerprint.
La valeur exacte de `model_dump()` envoyée à l'evaluator est persistée dans
`goal_contexts`, avec son digest, sa provenance et son compte approximatif. La
troncature est stable et conserve le schéma strict. Les résultats worker bruts
et les secrets ne traversent pas cette frontière.

## Strategy retrieval

`StrategyRetrieval` recherche séparément:

- jusqu'à deux épisodes de succès;
- jusqu'à deux épisodes d'échec;
- jusqu'à deux mémoires pertinentes.

Un hint d'épisode utilise objectif et tags d'échec seulement. Il ne lit ni
`plan_summary` ni `episode_steps`, ce qui empêche la copie implicite d'un plan
historique. Les items mémoire dont le type ressemble à `plan` sont ignorés.
Chaque hint est réduit à une seule leçon d'au plus 280 caractères et porte son
ID de provenance.

Les hints sont du contenu de contexte non autoritatif. Une ressemblance
sémantique ne rend pas un skill autorisé et ne prouve pas qu'une stratégie est
correcte aujourd'hui.

## Invariants testés

- même état → mêmes cartes, ordre, troncature, provenance et compte;
- limites mémoire/épisode/agent/upstream indépendantes;
- résultat de chaque nœud borné avant assemblage;
- budgets optionnels à zéro;
- secrets et chemins absents de la ligne persistée;
- source supprimée du pack absente de la provenance;
- payload planner/evaluator persisté identique au payload envoyé et digesté;
- guidance de replan et hints inclus avant, jamais après, le budget global;
- hints succès/échec/mémoire bornés et aucun ancien plan recopié.

## Limites

- L'approximation de tokens ne remplace pas un comptage tokenizer-specific.
- La pertinence des cartes est surtout déterministe/récente; il n'existe pas
  encore d'optimiseur de contexte évalué offline.
- `goal_contexts` n'a pas encore de purge/rétention opérateur qualifiée.
- Aucun contexte ne doit être utilisé pour décider une permission ou une
  action sensible depuis un cache mobile périmé.
