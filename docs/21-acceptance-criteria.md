# 21 — Acceptance Criteria

## Lecture des statuts

- **Couvert localement**: preuve automatisée sur le checkout actuel.
- **Partiel**: contrat ou composants présents, mais frontière réelle non exercée.
- **Roadmap**: non livré; aucune réussite ne doit être annoncée.

## Slice sécurisé `0.7`

### AC-001 — Appairage — Couvert localement, device partiel

Un code ne peut être émis que sur loopback avec le secret opérateur. Il est
unique, expirant, limité en tentatives et consommé une fois. Le jeton candidat est
lié à l'origine, au `device_id` et au `pairing_id`, stocké seulement sous forme de
digest côté serveur et limité au bootstrap/finalize avant bascule. L'ancien jeton
reste valide jusqu'au stockage durable du candidat et à sa première requête de
ressource; une reprise SecureStore couvre réponse perdue, écriture active échouée
et expiration du candidat. Le client refuse le HTTP distant et ne migre jamais une
ancienne paire URL/jeton séparée. Le parcours sur iPhone physique reste à exercer.

### AC-002 — Tâche Chat — Couvert localement, modèle live partiel

Un appareil authentifié peut créer une conversation et une tâche. Une réponse
modèle sans outil reste `planned` et `proposal_only`; elle ne devient pas
`completed`. Le modèle local réel reste à qualifier.

### AC-003 — Source de vérité — Couvert localement, device partiel

Ubuntu contrôle les statuts. Le bootstrap REST écrit tâches, approbations et
curseur dans un cache SQLite lié à l'origine actuellement jumelée; une autre
origine ne peut pas lire les anciens enregistrements. Le client WebSocket utilise
un ticket court à usage unique, se reconnecte avec backoff, redémarre après un
re-jumelage et déclenche un bootstrap autoritatif à chaque reconnexion. Les listes
de tâches et d'accords ainsi que le résumé d'une tâche peuvent utiliser le cache
hors ligne avec un avertissement explicite. Les décisions, annulations,
planifications et feedback restent verrouillés sans preuve fraîche. Ces parcours
sont couverts avec les frontières réseau/SQLite simulées; la persistance et la
reconnexion sur iPhone physique restent à exercer.

### AC-004 — Outbox offline — Roadmap

L'envoi offline et son replay ne sont pas implémentés dans ce slice.

### AC-005 — Proposition orchestrateur — Couvert localement

Le parseur exige un objet JSON avec `tool_name`, `arguments` et `summary`; un outil
inconnu ou des arguments invalides sont refusés avant persistance/exécution.

### AC-006 — Permission sensible — Couvert localement

Un write ou `process.run` crée atomiquement appel, approbation et transition de
tâche. L'approbation est liée à l'identifiant, l'outil et les arguments exacts;
l'interface montre une cible/commande expurgée et refuse une liaison invalide. Il
n'existe aucune route publique pour créer une approbation orpheline.

### AC-007 — Autoriser une fois — Couvert localement

La décision compare atomiquement approbation, appel et tâche. Une seule décision
gagne; tout replay reçoit `409` et ne répète pas l'exécution.

Une exécution abandonnée après revendication est réconciliée après redémarrage en
échec à résultat incertain. Un appel approuvé mais pas encore revendiqué est
réconcilié comme non démarré. Aucun des deux n'est rejoué automatiquement.

### AC-008 — Refus et annulation — Couvert localement

Un refus bloque l'appel. Une annulation invalide les appels et approbations en
attente; une tâche terminale ne peut pas être ressuscitée par une proposition.

### AC-009 — Mémoire — Couvert localement pour CRUD lexical

Création, lecture, recherche lexicale, épinglage et suppression sont disponibles.
Injection automatique de contexte vectoriel et traçage d'embeddings restent
roadmap.

### AC-010 — Capacité iPhone — Roadmap

Location, contacts, caméra et autres brokers natifs ne sont pas inclus et aucune
collecte de capteur n'est revendiquée.

### AC-011 — Feedback — Couvert localement, export roadmap

Un appareil peut enregistrer un feedback relié à une tâche ou un agent existant.
L'export de dataset n'est pas livré.

### AC-012 — Checks locaux — Couvert localement

`./scripts/check.sh` vérifie les prérequis puis exécute typage, lint, tests,
Expo Doctor, Ruff, mypy et Bandit. Cela ne remplace pas l'exécution Bubblewrap
Ubuntu, le modèle live ou le build/install/launch iPhone.

## Définition de terminé

Le slice source peut être déclaré validé lorsque les checks locaux et le contrat
OpenAPI passent. Une release opérationnelle exige encore:

- exécution de confinement Bubblewrap réelle sur l'hôte Ubuntu cible;
- endpoint modèle live et résultats d'exécuteur observés;
- terminaison TLS privée vérifiée;
- build, signature, installation, lancement et parcours sur iPhone physique;
- tests de reconnexion, restauration et persistance sur iPhone physique.

Tant que ces couches ne sont pas prouvées, le statut reste localement validé et
non « production ready ».
