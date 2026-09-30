# M05 — consultation passive des reçus de mémoire

Contrat implémenté localement le 29 septembre 2026 UTC, avec service de lecture passive, route authentifiée, OpenAPI, client strict et volet mobile. Aucun changement de schéma de base. La qualification locale et les essais visuels sur fixtures ne constituent pas un déploiement ni une validation sur iPhone physique.

## Périmètre implémenté

Montrer, depuis l’activité d’un projet, les souvenirs effectivement présents dans les reçus enregistrés : récupération, rattachement à un appel modèle, présence dans la charge d’un job. Ces preuves ne signifient ni compréhension du souvenir, ni réussite de l’opération, ni validation du contenu mémorisé.

La consultation ne récupère pas de nouveaux souvenirs, n’appelle aucun modèle, n’indexe rien, ne consomme aucun budget et ne crée pas rétroactivement de lien de projet. Elle reste disponible pour les buts terminés. L’import documentaire, le catalogue de toutes les collections et la promotion vers la mémoire générale nécessitent leurs propres parcours.

## Route

`GET /goals/{goal_id}/memory-usage?limit=20&cursor=...`

- Authentification d’appareil identique aux lectures du control plane ; pas de nouveau modèle de propriété par appareil inventé.
- `limit` entier entre 1 et 50 ; curseur opaque de 512 caractères maximum, lié au but demandé et à une clé de pagination strictement ordonnée. Curseur mal formé ou appartenant à un autre but : 400.
- But inexistant : 404. Un but existant sans lien a `project_id: null` ; le GET ne crée pas ce lien.
- Connexion SQLite `PRAGMA query_only=ON` et transaction de lecture ; filtres de but/projet avant les limites SQL.
- Réponse `Cache-Control: no-store`. Une panne n’est jamais remplacée par une collection vide.
- Pagination par clé `(recorded_at, id)` décroissante, avec plafonds d’insertion SQLite fixés dans le curseur pour les trois collections. Le curseur opaque compresse son enveloppe puis l’encode en base64url (maximum 512 caractères, décodage limité à 2 048 octets) ; il lie le but, son rattachement, les ancrages d’insertion et la dernière clé rendue. Une insertion antidatée postérieure reste hors fenêtre ; un ancrage supprimé ou un rattachement changé impose une actualisation. La portée et la sensibilité des sources sont réévaluées à chaque page. Les nouveaux événements n’apparaissent qu’après actualisation.
- Maximum 100 éléments par reçu et 128 Kio de JSON UTF-8 par page. Si la taille impose une réduction, la page s’arrête sur le dernier reçu rendu ; le curseur reprend après ce reçu. Un reçu trop volumineux voit ses éléments bornés et comptés dans `omitted_item_count`, sans disparition silencieuse.

## Exemple exact d’enveloppe

Tous les champs montrés sont requis. Une valeur non enregistrée reste `null`, sans estimation.

```json
{
  "schema_version": "1.0",
  "goal_id": "goal_a",
  "project_id": "project_a",
  "current_conversation_revision": 2,
  "observed_at": "2026-09-29T03:00:00Z",
  "availability": "available",
  "history_coverage": "recorded_receipts_only",
  "entries": [
    {
      "id": "model_call:gmc_a",
      "evidence_stage": "attached_to_model_call",
      "recorded_at": "2026-09-29T02:58:00Z",
      "completed_at": null,
      "status": "started",
      "purpose": "planner",
      "conversation_revision": 1,
      "task_id": "tsk_a",
      "node_id": null,
      "model_call_id": "gmc_a",
      "worker_job_id": null,
      "context_id": "ctx_a",
      "model_id": "model-alias",
      "retrieval": {
        "mode": "unknown",
        "reason": null,
        "provider_fingerprint": null
      },
      "items": [
        {
          "id": "pmem_a",
          "source_id": "gmsg_a",
          "source_kind": "message",
          "scope": "project",
          "source_goal_id": "goal_a",
          "source_revision_id": null,
          "source_at": "2026-09-28T23:00:00Z",
          "source_state": "unknown",
          "verification": "user_asserted",
          "summary": null
        }
      ],
      "omitted_item_count": 0
    }
  ],
  "next_cursor": null
}
```

`current_conversation_revision` est celle du but au moment de la lecture. Chaque entrée expose séparément `conversation_revision`, issue du reçu historique exact ou `null` si elle n’y est pas enregistrée. Une ancienne opération ne reprend jamais implicitement la dernière révision des consignes.

`observed_at` date le snapshot servi par le backend. `recorded_at` et `completed_at` proviennent du reçu. `source_at` provient de la source. Le mobile conserve séparément sa date de réception et marque un ancien résultat conservé après une panne ; aucune de ces dates n’est présentée comme la date de connaissance du modèle.

## Sens des champs et bornes

| Champ | Valeurs et règle |
| --- | --- |
| `availability` | `available` ou `no_records`. Le second signifie absence de reçus consultables, pas absence d’usage de mémoire. |
| `evidence_stage` | `retrieved`, `attached_to_model_call`, `included_in_worker_job`. Aucun statut « assimilé ». |
| `status` | État exact enregistré du job, de l’appel ou de la récupération, chaîne bornée à 50 caractères. Ce n’est pas le statut de vérification du souvenir. |
| `purpose` | Rôle/capacité enregistré, chaîne bornée à 100 caractères, sinon `null`. |
| `retrieval.mode` | `lexical`, `semantic`, `hybrid`, `unknown`. Seulement d’après un reçu exact, jamais selon le fournisseur actuellement configuré. |
| `retrieval.reason` | Code public du reçu, maximum 100 caractères, sinon `null`. Aucun message d’erreur réseau brut. |
| `provider_fingerprint` | Empreinte de 64 caractères hexadécimaux seulement si conservée dans ce reçu, sinon `null`. Aucune URL du fournisseur ou donnée d’authentification. |
| `source_kind` | `general_memory`, `message`, `project_plan`, `episode`, `unknown`. |
| `scope` | `general` ou `project`, déduite du rattachement autoritaire ; aucune portée fournie par un texte de modèle. |
| `source_state` | `available`, `changed`, `missing`, `redacted`, `unknown`. L’existence actuelle d’une ligne ne prouve pas qu’elle correspond à la version lue. Sans version/hash fiable, l’état reste `unknown`. |
| `verification` | `user_asserted`, `assistant_claim`, `recorded_outcome`, `unknown`. Qualifie la provenance, pas une validation sémantique. Pour `mem_*`, `user_asserted` exige l’événement exact `memory.remembered`, acteur `device` et même identifiant ; sans cette preuve, `unknown`. Le type, la portée et le score de confiance ne prouvent pas l’auteur. |
| `summary` | Extrait public vérifiable du reçu, maximum 800 caractères ; présentation initiale de 280 caractères dans l’app. `null` si supprimé, masqué, hors portée ou non vérifiable. Une citation exacte d’un reçu intègre peut rester affichable avec `source_state: unknown` : elle prouve ce texte enregistré, pas la version de la source consultée. Aucun remplacement par le texte actuel présenté comme ancien texte utilisé. |
| Identifiants | Chaînes stables bornées selon les identifiants existants, maximum 200 caractères ; `null` lorsqu’aucune relation fiable n’est enregistrée. |
| `omitted_item_count` | Nombre réel d’éléments du reçu écartés par les bornes de sortie. Ce n’est pas un compteur estimé de mémoire totale. |

## Raccordements autoritaires

1. **Récupération** : `goal_memory_queries.context_json` contient le reçu du planificateur ou évaluateur. La présence de ce reçu ne prouve pas qu’il a été transmis à un modèle. Les lignes d’embeddings sans contexte enregistré ne sont pas converties en succès de récupération.
2. **Appel enregistré** : jointure exacte `goal_model_calls.context_id → goal_contexts.id`, avec même but, tâche racine, nœud et rôle. Si un nœud est enregistré, sa tâche doit avoir la source exacte `goal:<id>`. Les identifiants et la révision conservés dans le JSON doivent correspondre au reçu historique ; on ne les remplace pas par la révision actuelle. Les anciens contextes qui ne portent pas ces champs restent lisibles. Seules les cartes mémoire et leurs références vérifiables sont projetées. Un appel réservé peut échouer avant toute transmission ; le libellé reste « Joint à un appel enregistré ».
3. **Job d’agent** : `plan_nodes.worker_job_id → agent_jobs.id`, même but, tâche, type de nœud `worker` et compétence exacte ; la tâche porte la source exacte `goal:<id>`. Ces conditions filtrent aussi l’ancrage de pagination des jobs. Seul le champ mémoire de la charge immuable est projeté. La présence dans le job ou sa réclamation ne prouve pas une lecture par le modèle. La génération de bail ne devient pas une preuve d’inférence.
4. **Sources** : `mem_`, `pmem_` et `ep_` sont résolus depuis leurs tables et liens autoritaires. Aucun rapprochement par date, similarité de texte ou nom de modèle.

Le planificateur peut ne conserver que les références de cartes, sans mode de récupération. Ce mode reste alors `unknown`. Le contexte d’évaluation et certains jobs de code conservent leur mode exact. Les charges de recherche/rédaction historiques ne gagnent pas un reçu mémoire rétroactif.

## Confidentialité et historique

La même frontière que M11 s’applique : souvenirs explicitement généraux (`general`, alias historique `global`) et projet exact. Les anciens contextes peuvent contenir une fuite de portée antérieure ; leur texte n’est pas réexposé. Une source maintenant sensible, supprimée ou hors portée est masquée même si un ancien contexte contient encore son texte. Les métadonnées d’un projet étranger ne sont pas révélées pour expliquer le masquage.

Les lignes sources et les reçus ne sont ni réécrits ni supprimés par cette consultation. La cohérence d’un effacement de tous les dérivés appartient au lot M11 de rétention/effacement. Les changements de source après la fin du snapshot ne sont pas empêchés par un GET ; chaque actualisation revérifie les autorisations et relations.

Ne jamais retourner `context_json` entier, une réponse de raisonnement interne, un vecteur, la requête originale brute, une URL privée ou des identifiants d’authentification.

## Validation locale

- But terminé et but sans lien consultables, sans écriture ni appel d’embeddings ; aucun lien créé par GET.
- Reçu récupéré mais jamais joint à un appel ; contexte joint mais appel échoué ; job en attente contenant la mémoire. Les trois preuves restent distinctes.
- Jointures erronées entre but/tâche/nœud/job/contexte rejetées ; aucun rapprochement temporel.
- Mode inconnu quand absent ; fournisseur configuré mais jamais utilisé ; repli lexical enregistré ; résultat hybride exact.
- Source déplacée, sensible, supprimée, corrigée et source sans version fiable ; aucun ancien texte hors portée rendu.
- Éléments généraux et épisodes de projets distincts, avec identifiants proches et saturation avant LIMIT.
- Pagination stable sur dates égales, curseur d’un autre but, curseur surdimensionné, insertions concurrentes et limite réelle de 128 Kio. Les éléments omis sont comptés.
- Mobile : annulation/réponse tardive après changement de projet ou connexion, réception séparée des dates serveur, ancien reçu conservé et signalé après échec d’actualisation.

## Intégration locale

Service et contrats dédiés `memory_inspection.py` / `memory_inspection_contracts.py`, route/OpenAPI et client/parser strict mobile sont raccordés. La vue ne charge les reçus qu’à son ouverture explicite et n’enregistre pas leur contenu dans le cache persistant. Le schéma strict Activity v1 reste intact. Projet → Activité ouvre « Mémoire utilisée » ; le catalogue général/projets/épisodes/documents constitue une étape distincte de M05.

Les tests durables sont `server/tests/test_memory_inspection.py`, les quatre schémas de `server/tests/test_openapi_route_coverage.py`, `mobile/src/lib/api/memory-usage.test.ts` et `mobile/src/components/memory-usage.test.tsx`. Ils couvrent également le JSON d’évaluation contradictoire, la compétence du job différente de celle de l’étape, un nœud de synthèse portant un job et la préservation d’un reçu historique de révision 1 lorsque les consignes actuelles sont à la révision 2. Les résultats précis de la qualification sont consignés dans le bilan du lot ; aucun statut de production n’en est déduit.
