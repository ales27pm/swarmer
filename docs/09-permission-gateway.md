# 09 — Permission Gateway

> **IMPLEMENTED — slice `0.9.0`:** approbations ponctuelles non rejouables pour
> `workspace.write_text` et `process.run`, lectures validées par règle, ainsi
> que grants iPhone courts, liés au digest et consommables une fois. `Allow
> rule` et l'édition de scope restent **PLANNED**. Le comportement exécutable est
> défini dans `server/app/services/execution_engine.py`,
> `server/app/services/iphone_capability_service.py` et
> `configs/permissions.yaml`.

## Objectif

Empêcher les dégâts sans transformer les modèles en policiers moralisateurs.

## Règle

> Les modèles proposent. La gateway décide. Les executors agissent.

## What “abliterated where it thinks, strict where it acts” means

- Orchestrateur et workers LLM peuvent être abliterated.
- Ils ne sont jamais détenteurs des permissions finales.
- Ils doivent produire une demande d'action structurée.
- La gateway applique des règles déterministes.
- Si l'action exige l'utilisateur, l'app iPhone reçoit une approval card.

## Action classes

| Class | Examples | Default |
|---|---|---|
| `read_safe` | lire fichiers projet explicitement autorisés | allow |
| `write_project` | modifier fichier dans repo autorisé | ask |
| `execute_safe` | lancer tests non destructifs | ask/allow rule |
| `network_read` | recherche web/docs | ask/allow rule |
| `network_write` | envoyer email, publier, push | ask |
| `device_sensitive` | localisation/photos/contacts/calendrier | ask |
| `destructive` | delete, rm, wipe, overwrite large | deny/ask elevated |
| `credential` | lire secret/token/password | deny by default |

## Permission decision

```json
{
  "decision": "ask",
  "risk": "medium",
  "reasons": ["writes_project_file"],
  "approval_required": true,
  "approval_ttl_seconds": 300
}
```

## Risk engine inputs

- action type;
- target path/resource;
- data sensitivity;
- agent identity;
- user intent match;
- previous approvals;
- command diff;
- network destination;
- file count/size;
- destructive verbs;
- credentials involved.

## Refusal normalizer

But: si un modèle répond “je ne peux pas”, le système ne doit pas bloquer bêtement. Il convertit en une demande claire:

```json
{
  "type": "permission_request",
  "reason": "Model refused or hesitated. Gateway requires explicit user decision.",
  "requested_action": {...},
  "risk": "unknown"
}
```

Important: le normalizer ne contourne pas la gateway. Il transforme le refus en objet évalué.

## Approval UI

L'utilisateur doit voir:

- le demandeur authentifié qui a initié la proposition;
- le motif et l'identifiant de la règle `ask` réellement évaluée;
- un résumé expurgé des données potentiellement touchées;
- l'identifiant monotone de l'événement `approval.requested`;
- action dérivée de l'appel, et non du résumé libre du modèle;
- cible exacte ou commande expurgée;
- identifiant d'appel et empreinte SHA-256 des arguments canoniques;
- risque;
- expiration;
- boutons.

Le contenu d'un fichier à écrire et les arguments arbitraires potentiellement
sensibles ne sont rendus ni dans la carte, ni dans les objets publics d'appel
d'outil, ni dans les événements WebSocket. Leur longueur ou leur nombre peut
être affiché, mais leurs valeurs exactes restent internes à la liaison et à
l'exécuteur. L'empreinte lie quand même l'accord à ces valeurs exactes. Une
liaison invalide désactive l'autorisation et est refusée à nouveau par le
serveur.
Le demandeur, la décision de politique et le résumé des données touchées sont
figés avec la demande. Leur événement d'audit est inséré dans la même
transaction SQLite que l'approbation, l'appel et la transition de tâche. Le
texte libre du modèle est remplacé, pour tout appel d'outil, par un libellé
serveur fixe affiché dans un bloc séparé; il ne peut donc ni recopier les
arguments sensibles ni faire partie des preuves de consentement.

L'app dérive également l'expiration de `expires_at`, réévalue l'échéance quand
elle redevient active et désactive localement les décisions expirées. Le serveur
reste l'autorité et conserve son refus `409` pour une décision expirée, rejouée
ou dont le contexte de consentement ne correspond plus à l'audit. La transition
d'expiration et son événement `approval.expired` sont commis dans la même
transaction; une panne d'audit laisse l'approbation en attente pour une nouvelle
matérialisation sûre.

Actions:

- Allow once;
- Deny;
- View details.

`Allow rule` et l'édition de scope restent roadmap.

## Grants de capability iPhone — IMPLEMENTED

Les règles `capability_rules` couvrent exactement les six capabilities
`iphone.*` actuellement supportées et ont toutes la décision `ask`. Elles sont
séparées des règles d'outils: une permission de lecture du workspace ne donne
aucun droit sur l'iPhone.

La demande est créée uniquement par un agent authentifié qui prouve une job et
une lease encore actives. Le serveur lie atomiquement:

- l'identifiant de demande, de tâche, de job, d'agent et de génération de lease;
- l'iPhone ciblé, choisi par la source authentifiée de la tâche, ou par l'unique
  appareil jumelé quand ce choix reste non ambigu;
- le nom de capability, les arguments canoniques et leur digest SHA-256;
- un fingerprint interne qui déduplique les créations identiques pour cette
  job et cette génération tant qu'elles restent non terminales;
- la règle, le risque, l'expiration, l'identifiant d'approbation et l'événement
  d'audit monotone.

Le WebSocket `iphone.capability.requested` ciblé contient `request_id`,
`capability_name`, `expires_at` et `preview.arguments_redacted: true`, sans
arguments. `iphone.capability.updated` contient uniquement `request_id`.
L'iPhone relit le détail autoritatif avec son bearer, puis `approve` ou `deny`.
Une approbation retourne un grant opaque seulement dans cette réponse; les
lectures exposent toujours `grant: null`. Si cette réponse est perdue, répéter
`approve` avant consommation fait tourner atomiquement le grant et invalide le
secret précédent. Une décision différente, un grant déjà consommé ou un état
terminal ne sont pas rejoués. Le serveur ne conserve que le digest du grant
actuel. Celui-ci expire au plus tôt entre la demande et le TTL de grant (90
secondes par défaut), porte `use: once` et reste lié au device, à la capability,
à l'approbation et au digest exact.

Avant tout appel Location/Contacts/Calendar/Photos/Mail/SMS, l'iPhone doit
consommer le grant via le serveur. Une seconde consommation, une lease perdue,
une tâche terminale, un mauvais device ou un digest différent reçoit `409`. Le
résultat natif doit ensuite respecter le nom et le statut autorisés. Une
nouvelle livraison strictement identique du résultat est reconnue comme
`duplicate`; un résultat terminal différent est refusé. L'expiration de lease
ou l'annulation de tâche annule aussi les demandes non terminales liées. La
valeur native reste dans la table SQLite de résultats pour le poll du worker;
l'audit publie la corrélation, le statut et un marqueur d'expurgation; le board
ne publie que la corrélation et le statut.

Cette voie ne partage pas les lignes `approvals` des appels d'outils et ne
permet jamais à un worker de prendre la décision utilisateur. Les transitions,
preuves d'audit et publications d'outbox restent transactionnelles en SQLite.

## Permission rules

Voir `configs/permissions.yaml`.

Exemples:

```yaml
rules:
  - id: allow-read-projects
    match:
      action: file.read
      path_prefix: /home/ales27pm/projects/
    decision: allow

  - id: ask-write-projects
    match:
      action: file.write
      path_prefix: /home/ales27pm/projects/
    decision: ask

  - id: deny-secrets
    match:
      path_glob: "**/.env*"
    decision: deny
```

## Sandbox

Executors doivent limiter:

- working directory;
- env vars;
- network;
- timeout;
- file access;
- max output;
- process tree kill.

Dans le slice actuel, Bubblewrap monte tout le workspace configuré en
lecture-écriture, sauf les chemins protégés masqués par des montages privés.
`cwd` est uniquement le répertoire de départ du processus et ne constitue pas
une limite de portée. La carte l'énonce explicitement avant consentement.

Les symlinks et les alias par lien physique d'un chemin protégé doivent échouer
fermés avant lecture, écriture ou lancement d'un processus.

Les arguments de processus sont intégralement expurgés des objets `ToolCall`
publics. Les sorties `stdout` et `stderr` sont conservées en interne comme preuve
d'exécution, mais les réponses HTTP, le bootstrap et les événements WebSocket
n'en exposent que la taille. Une erreur de persistance après dispatch est
annoncée comme `tool.outcome_uncertain` avec `409`; elle ne devient jamais une
fausse réussite ni une invitation à rejouer l'accord consommé.

## Audit

Chaque demande et décision:

```json
{
  "event": "permission.decision",
  "approval_id": "apv_...",
  "task_id": "tsk_...",
  "agent_id": "code-worker-01",
  "decision": "allow_once",
  "user_id": "ales27pm",
  "timestamp": "...",
  "hash_prev": "...",
  "hash": "..."
}
```

Dans le slice `0.9.0`, `audit_id` exposé sur l'approbation désigne directement
l'identifiant entier de son événement durable `approval.requested`; il ne s'agit
ni d'un texte du modèle ni d'un identifiant synthétique de l'interface.
L'événement de décision est écrit dans la même transaction que la consommation
de l'approbation; un refus y ajoute aussi `tool.denied`. Les événements terminaux
`tool.completed` et `tool.failed` sont écrits dans la même transaction que
l'état terminal durable correspondant.

## Anti-bullshit rules

- Une permission verbale dans un prompt ne suffit pas pour action sensible.
- Un agent ne peut pas s'accorder une permission.
- Un modèle abliterated ne remplace jamais les règles.
- Les credentials sont jamais injectés dans le prompt.
- Les actions externes sont visibles avant exécution.
