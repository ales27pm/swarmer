# 12 — API Contracts

## Contrat courant

Le MVP local expose l'API `0.8.0`. [`api/openapi.yaml`](../api/openapi.yaml) est
la source machine-readable; cette page résume les règles qui ne doivent pas être
perdues par un client.

Le trafic réseau authentifié utilise HTTPS/WSS. HTTP/WS en clair est accepté sur
loopback seulement, sauf dérogation de développement explicite.

## Authentification et appairage

Un code ne peut être émis que depuis loopback avec le secret opérateur:

```http
POST /pairing/code
X-Mongars-Operator-Token: <secret-operateur>
```

Le téléphone échange ensuite le code sans envoyer de jeton d'un autre serveur:

```http
POST /pairing/complete
Content-Type: application/json

{
  "code": "123456",
  "device_id": "iphone-ales",
  "name": "Mon iPhone"
}
```

Le code est unique, expirant, à tentatives limitées et consommé une fois. La
réponse contient un `candidate_token`, un `pairing_id`, le `device_id` lié et une
expiration courte. Ce bearer candidat ne peut appeler que `GET /sync/bootstrap`
et `POST /pairing/finalize`. Le client vérifie le bootstrap, finalise exactement
la paire, puis écrit une reprise liée dans SecureStore avant un second bootstrap
qui effectue la bascule. Toutes les routes de ressources utilisent ensuite:

```http
Authorization: Bearer <device-token-opaque>
```

Le client mobile refuse une origine HTTP non loopback avant tout appel. Le serveur
ne remplace jamais l'ancien jeton lors de l'échange ou de la finalisation: la
première requête ordinaire du candidat finalisé effectue seule la bascule atomique.
Le candidat est d'abord durable dans une clé SecureStore séparée. Une réponse de
bascule perdue est reprise avec ce bearer; son expiration restaure la connexion
active conservée. L'origine n'est jamais dissociée de son jeton.

Le serveur dérive l'identité de `source` depuis ce principal. Le client ne peut
pas la fournir dans `TaskCreate`.

## Ressources

```text
GET    /health
POST   /pairing/code
POST   /pairing/complete
POST   /pairing/finalize

POST   /tasks
GET    /tasks
GET    /tasks/{task_id}
POST   /tasks/{task_id}/cancel
POST   /tasks/{task_id}/plan
POST   /tasks/{task_id}/tool-calls
GET    /tasks/{task_id}/tool-calls

POST   /chat
GET    /conversations
GET    /conversations/{conversation_id}/messages
GET    /sync/bootstrap

GET    /approvals
POST   /approvals/{approval_id}/decision

GET    /memory
POST   /memory
POST   /memory/search
PATCH  /memory/{memory_id}
DELETE /memory/{memory_id}

GET    /agents
GET    /agents/{agent_id}
POST   /agents/register
POST   /agents/{agent_id}/heartbeat

GET    /audit
POST   /feedback
POST   /ws/ticket
WS     /ws?ticket=<ticket-court>
```

`GET /audit` renvoie une projection publique authentifiée. Les erreurs de
processus héritées sont expurgées à la lecture sans modifier les lignes internes
append-only. `prev_hash` et `hash` attestent ces lignes internes brutes; un
payload public expurgé n'est donc pas directement recalculable côté client.

## Exemples déterminants

Créer une tâche ne revendique aucune exécution:

```http
POST /tasks
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "input": "Inspecte le projet et trouve les erreurs",
  "mode": "review",
  "conversation_id": "conv_..."
}
```

Une réponse modèle `tool_name: none` laisse la tâche `planned` et son message est
marqué `proposal_only`. Seul le résultat réussi d'un exécuteur pris en charge peut
produire `completed`.

Décider une approbation:

```http
POST /approvals/{approval_id}/decision
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "decision": "allow_once",
  "user_note": "Autorisé pour cet appel seulement"
}
```

`approve` est un alias de `allow_once`; aucune règle permanente n'est créée. Un
replay, une course perdue, une expiration ou l'annulation de la tâche produit
`409` et ne relance pas l'outil.

Chaque objet d'approbation retourne aussi `tool_call_id`, `action_digest`,
`binding_valid`, `action_preview`, `requester`, `policy`,
`affected_data_summary`, `audit_id` et `consent_context_valid`. `requester`
provient du principal appareil authentifié qui a initié la proposition;
`policy` est l'instantané de la règle exécutable `ask`; `audit_id` référence
l'événement durable `approval.requested` créé dans la même transaction.
L'empreinte couvre l'identifiant, le nom
d'outil et les arguments canoniques exacts; l'aperçu n'expose que les éléments
nécessaires au consentement (cible, commande autorisée, tailles et champs
expurgés). Pour tout appel d'outil, le résumé libre du modèle est remplacé par
un libellé serveur fixe avant persistance ou publication afin qu'il ne puisse
pas recopier une valeur sensible. Les anciennes lignes sans contexte vérifiable
retournent des valeurs nulles et `consent_context_valid: false`; elles échouent
fermées.

Dans toutes les réponses publiques, `ToolCall.arguments` est une projection
serveur expurgée: le contenu d'écriture et les arguments arbitraires de processus
sont remplacés par des métadonnées de masquage. Les arguments exacts persistent
localement pour la liaison et l'exécution mais ne transitent pas par les routes,
le bootstrap ou les événements WebSocket.

Pour `process.run`, `ToolCall.result` masque aussi intégralement `stdout` et
`stderr` par leur nombre d'octets. Si l'effet a pu se produire mais que son état
terminal et son audit n'ont pas pu être commis, l'API retourne `409`, diffuse
`tool.outcome_uncertain`, interdit le replay et laisse la récupération au
redémarrage clore l'état avec un audit atomique.

La recherche mémoire actuelle est lexicale:

```http
POST /memory/search
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "query": "projet extérieur",
  "scope": "project"
}
```

Les résultats portent `search_kind: lexical`; aucune recherche vectorielle n'est
revendiquée.

## Agents et WebSocket

`POST /agents/register` exige un appareil authentifié. L'agent est créé
`unverified` et reçoit une credential dédiée retournée une seule fois. Seule
cette credential peut appeler son endpoint heartbeat.

Un téléphone authentifié demande un ticket WebSocket de 30 secondes:

```http
POST /ws/ticket
Authorization: Bearer <device-token>
```

Il ouvre ensuite `wss://<serveur>/ws?ticket=<ticket>`. Le ticket est consommé une
fois; le jeton longue durée n'apparaît jamais dans l'URL.

## Erreurs

Les erreurs HTTP suivent actuellement la forme FastAPI:

```json
{
  "detail": "description de l'erreur"
}
```

Les clients doivent traiter explicitement `401`, `409`, `422`, `426` et `502`.
Les anciennes routes `sync/pull`, `sync/push`, `iphone/capability-result` et le
WebSocket par `device_id` ne font pas partie du contrat `0.8.0`.

Le gate local valide le métaschéma OpenAPI, les routes, statuts générés, corps de
requête, paramètres, sécurité et schémas de réponse clés. Les JSON Schemas dans
`schemas/` sont aussi validés contre des payloads réellement retournés; le schéma
`sync-operation` est explicitement roadmap et n'est relié à aucune route actuelle.
