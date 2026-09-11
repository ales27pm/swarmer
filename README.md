# monGARS Swarm App — Build Documents

Version: 0.12.0 autonomous swarm runtime
Date: 2026-09-08
Owner: ales27pm / 27PM  
Target: iPhone Expo app + Ubuntu local AI control plane + distributed autonomous swarm

## But

Ce paquet contient les documents nécessaires pour construire l'application **monGARS Swarm App**:

- application iPhone en Expo/React Native;
- modèle local on-device pour orchestration légère ou interface intelligente;
- control plane Ubuntu comme source de vérité;
- swarm d'agents autonomes distants;
- state partagé, conversations, mémoire structurée locale et feedback loop;
- stratégie “abliterated où ça pense, strict où ça agit”.

## Principe central

Les modèles ne sont pas la couche de sécurité. Les modèles proposent des plans, appellent des outils ou demandent une permission. La **Permission Gateway** applique les règles, demande confirmation à l'utilisateur et contrôle les exécuteurs.

> Modèles = pensée et délégation.  
> Gateway = permission, risque, audit.  
> Executors = actions sandboxées.  
> Ubuntu = vérité officielle.  
> iPhone = interface, approbations, capteurs, cache local.

## État implémenté

Le control plane local conserve ses fondations réelles d'authentification, de
modèle et d'exécution:

- émission d'un code d'appairage uniquement depuis une connexion loopback ayant
  aussi le secret opérateur; code unique, expirant, à tentatives limitées;
- jeton opaque par appareil pour toutes les API de ressources, conservé sous
  forme de digest côté serveur; un nouvel appairage crée d'abord un candidat
  court, limité au bootstrap et à sa finalisation. L'ancien jeton reste valide
  jusqu'à ce que le candidat soit durable dans SecureStore puis utilisé pour un
  bootstrap de bascule. Une trace SecureStore séparée permet de reprendre une
  réponse perdue ou de revenir à la connexion active si le candidat expire. Les
  anciennes clés URL/jeton séparées ne sont jamais recombinées;
- ticket WebSocket de 30 secondes, consommable une seule fois, afin de ne jamais
  mettre le jeton d'appareil longue durée dans une URL. Son émission revalide
  atomiquement le bearer et la lignée de jumelage sous le writer lock. Un
  re-pair supprime les tickets inutilisés et change cette lignée; même un
  WebSocket déjà établi est revérifié à chaque envoi/réception et fermé au
  prochain échange s'il appartient à l'ancienne session. Un identifiant de
  connexion durable limite en plus chaque appareil à un seul propriétaire de
  livraison, même entre processus du control plane; les entrées/sorties socket
  sont bornées et le fan-out inter-appareils est concurrent afin qu'un pair lent
  ne puisse pas retenir une bascule ni retarder tous les autres. Un journal
  SQLite de notifications uniquement métadonnées et ses checkpoints par
  instance relaient les invalidations entre processus; un checkpoint de crash
  expiré déclenche un nouveau bootstrap, jamais le rejeu d'un effet;
- souscription WebSocket mobile avec reconnexion bornée, pause en arrière-plan
  et nouveau ticket à usage unique à chaque connexion. Une reconnexion force un
  bootstrap REST autoritatif afin de combler les événements manqués;
- mutations SecureStore de jumelage sérialisées et promotions protégées par
  comparaison de la trace pending exacte et de sa génération; une reprise
  ancienne ne peut ni remplacer la connexion courante ni supprimer un re-pair
  plus récent;
- cache SQLite limité à l'origine actuellement jumelée. Les listes et détails
  mis en cache sont signalés comme périmables hors ligne, et toutes les actions
  sensibles restent verrouillées sans preuve serveur fraîche;
- tâches enrichies, conversations/messages, appels d'outils, approbations,
  mémoire, agents, audit chaîné par hash et feedback;
- message board durable SQLite et outbox transactionnelle dans la même base que
  l'état. Chaque drainer revendique atomiquement un lot avec une identité
  d'instance, une lease de publication et une génération de fencing. Un
  publisher périmé ne peut donc pas marquer la publication d'une génération
  plus récente. Une clé de déduplication applicative obligatoire rend la
  livraison au board rejouable, y compris après un crash survenu entre
  publication et `published_at`. Les jobs sont revendiqués atomiquement par compétence,
  avec une seule exécution distante active par tâche, capacité déclarée,
  credential agent, lease opaque expirante et génération de fencing. Le reaper
  ne remet automatiquement en file que `workspace.list_dir` et
  `workspace.read_text`, dans une limite de tentatives et seulement si la
  génération expirée n'a eu aucune activité de capability iPhone. Toute autre
  expiration est traitée comme un résultat potentiellement incertain, échoue et
  est auditée. Les heartbeats renouvellent toujours l'état autoritatif, mais leur
  publication durable est coalescée par job et génération;
- runner de maintenance continuellement fenced: il renouvelle sa lease avant
  50 % du TTL et revérifie propriétaire et génération dans chaque transaction
  autoritative. Une perte de renouvellement annule le travail; le prochain lot
  de mutations de l'ancien propriétaire est refusé après une reprise en
  génération `N+1`;
- contrat de transport `DurableEvent` indépendant du backend et adaptateur
  Redis Streams optionnel. SQLite demeure le backend par défaut et la seule
  source de vérité; Redis ne contient que des notifications, conserve les
  identifiants applicatifs et la `dedupe_key`, et une indisponibilité laisse
  l'outbox en attente jusqu'à récupération. La rétention est bornée et ne
  remplace jamais une reconstruction depuis SQLite/API: chaque nouvelle
  publication non dédupliquée applique `MAXLEN ~` et un trim d'âge `MINID ~` au
  stream visé, puis renouvelle
  le TTL d'inactivité du stream. Chaque `dedupe_key` est liée séparément par une
  clé Redis nommée avec son SHA-256 et son propre TTL `PX`; aucun hash ou index
  global n'est utilisé. Le trim d'âge est donc opportuniste à la
  publication, tandis qu'un stream entièrement inactif peut expirer; aucun
  consumer ne peut supposer un historique Redis infini. Une
  publication plus longue que sa lease peut réussir dans Redis puis être
  rejouée par la génération suivante: la livraison est explicitement **au moins une fois**,
  jamais exactement une fois. Les doublons sont neutralisés par la
  déduplication applicative et comptés avec les expirations de claim et les
  latences de publication;
- identité aléatoire par démarrage de chaque instance du control plane, avec
  heartbeat persistant, et leases singleton à génération pour le reaper, les
  expirations de capabilities, l'entretien de l'outbox et le recalcul de score.
  Une fondation de consumers de confiance conserve checkpoint, retry borné,
  dead letter et ack après succès; aucun worker ne reçoit pour autant des
  identifiants Redis ni un accès direct au broker;
- recherche mémoire lexicale conservée avec architecture d'embeddings et ranking
  hybride lorsqu'un provider est configuré. Un index FAISS local optionnel et
  reconstruisible peut être régénéré depuis les embeddings SQLite; sa perte ou
  sa corruption ne détruit aucune mémoire et le chemin lexical continue de
  fonctionner. La projection vérifie propriétaire UID, modes privés, types,
  liens et intégrité, conserve un nombre borné de générations et préserve le
  pointeur publié lors d'une interruption ou d'un manque d'espace. Au rebuild,
  elle nettoie sous lock uniquement les orphelins privés et possédés laissés par
  un crash (`.tmp-*`, `.CURRENT-*`, `.digest-*`); tout artefact inattendu ou
  non sûr fait échouer l'entretien sans suppression;
- réplica iPhone étendue aux tâches, approbations, appels d'outils,
  conversations/messages, agents, mémoire épinglée et métadonnées d'audit. Cette
  réplica n'autorise jamais une action sensible;
- outbox de mutations mobile, locale et liée à l'origine, pour trois opérations
  rejouables seulement: feedback, épinglage de mémoire et message de chat sans
  création de tâche. Le serveur lie `Idempotency-Key`, identité de l'appareil et
  digest canonique dans un reçu atomique. Le drain ne commence qu'après un
  bootstrap autoritatif réussi et revérifie l'origine et le bearer capturés. Un
  changement de jumelage abandonne les anciennes entrées. Décisions
  d'approbation, grants/exécution iPhone, composeurs, `process.run` et toute
  action sensible sont refusés par cette outbox. Les drains sont sérialisés par
  origine, bornés dans le temps et continuent après avoir isolé une entrée
  définitivement invalide;
- flotte exemple composée des workers Files, Research et Code Review. Le
  Research Worker ne reçoit jamais d'URL de job: il utilise un unique adaptateur
  HTTPS configuré par l'opérateur, avec limites de temps/taille et contenu
  marqué non fiable. Le Code Review Worker construit uniquement des appels Git
  et Ruff en lecture, sans shell, write ni push. Il épingle l'identité de la
  racine et du dépôt, revalide le snapshot, désactive hooks, helpers, config Git
  hôte et récupération paresseuse, assainit l'environnement et borne
  temps/sorties/fichiers. Un budget monotone unique couvre snapshot et commandes,
  avec 64 MiB au total, 16 MiB par fichier et une profondeur maximale de 64.
  Le Research Worker épingle une destination HTTPS
  publique validée, refuse redirects, credentials URL, adresses
  privées/loopback/link-local, changement d'adresse résolue et réponses lentes,
  surdimensionnées ou décompressées hors limite. Un seul deadline monotone
  couvre DNS, connexion, TLS, requête, headers et lecture, et le resolver DNS
  est single-flight afin qu'un timeout ne crée pas une accumulation de threads.
  Les cartes d'agents sont
  validées par une allowlist serveur; une compétence inconnue, privilégiée ou
  incompatible est refusée. La règle opérateur courante est réévaluée à
  l'inscription, à chaque claim et avant toute redistribution; une compétence
  révoquée met en quarantaine un job en file et empêche tout nouveau claim. Une
  lease déjà active peut terminer sous l'autorisation qui l'a créée; si elle
  expire après révocation, le job est mis en quarantaine et n'est jamais
  redistribué. La règle worker autoritative et son epoch monotone sont persistés
  dans SQLite. Un reload capture l'epoch attendu avant parsing puis effectue un
  compare-and-swap sous le writer lock; un candidat périmé ne peut pas restaurer
  un allow après un deny plus récent. Inscription, mise en file, claim et reaper
  relisent la projection durable dans leur transaction: ni un cache allow
  périmé, ni un cache deny périmé ne remplace l'autorité SQLite;
- scheduler déterministe v2: compatibilité de protocole et de compétence,
  agent `online` avec heartbeat encore frais, capacité disponible, ratio de
  charge, score observé, latence
  après un minimum d'échantillons, ancienneté puis identifiant stable. Chaque
  décision persiste une preuve expurgée. Les scores proviennent des résultats,
  expirations et feedback observés par le serveur, jamais de l'auto-évaluation
  d'un worker;
- transport corrélé du Capability Broker pour position, contacts, calendrier,
  sélection de photo et composition mail/SMS. Un worker ne peut créer ou sonder
  une demande qu'avec sa lease active. Seul l'iPhone ciblé peut décider, puis
  consommer un grant opaque, court et à usage unique, lié au digest exact de
  l'action, avant d'appeler l'API native. Une reprise d'approbation avant
  consommation fait tourner ce grant et invalide le secret précédent.
  L'événement WebSocket initial expose
  seulement l'identifiant, le nom de capability, l'expiration et un marqueur
  d'arguments expurgés; les mises à jour n'exposent que l'identifiant. Le détail
  autoritatif vient de REST. Mail et SMS
  ouvrent une composition visible et ne sont pas envoyés silencieusement. Le
  code et les contrats sont implémentés; les dialogues de permission et
  l'exécution sur iPhone physique restent à prouver séparément;
- le workflow mobile de capability reste lié à l'origine serveur et au bearer
  d'appareil exacts observés lors de son démarrage. L'app ne persiste ni grant ni
  résultat sensible; elle ne conserve en mémoire qu'une reprise bornée du POST
  de résultat après exécution native. Une terminaison de l'app entre l'action
  iOS et ce POST laisse donc l'issue inconnue côté Ubuntu, sans réexécution
  automatique;
- export JSONL de corrections revues avec expurgation de chemins protégés et de
  secrets;
- sérialisation centralisée et fermée des événements partagés: aucun bearer,
  token de lease/grant, argument ou résultat natif sensible, contenu de
  contact/localisation/mail/SMS ni contenu de chemin protégé n'entre dans le
  board, Redis ou le WebSocket générique. `/status` expose seulement version,
  identité d'instance, santé du backend et compteurs opérationnels. Les
  compteurs cumulés d'expiration/retry/dead-letter worker et ceux de
  publication outbox sont des singletons persistants mis à jour avec leurs
  transitions; la lecture de statut ne reparcourt pas les historiques
  append-only;
- planification par le modèle local sans minuterie ni succès simulé: seul un
  résultat réel de l'exécuteur peut terminer une tâche;
- module iOS local en development build pour Core ML, MLX et llama.cpp/GGUF.
  Le module conserve les modèles dans le conteneur privé, ne reçoit ni URL ni
  bearer du control plane et ne retourne que du texte. L'app rejette toute
  sortie qui ne respecte pas exactement le contrat de proposition JSON; une
  action acceptée est ensuite soumise explicitement à l'API authentifiée et
  reste soumise aux règles, accords et preuves serveur;
- création appel-outil/approbation et décisions atomiques, non rejouables. Une
  tâche annulée ou terminale ne peut pas être ressuscitée. « Autoriser une fois »
  n'installe aucune règle persistante. Chaque accord est lié à l'identifiant,
  au nom et aux arguments canoniques exacts de l'appel; l'iPhone affiche une
  cible/commande expurgée et l'empreinte. Le texte libre du modèle est remplacé
  par un libellé serveur fixe pour tout appel d'outil et ne devient jamais une
  base de consentement;
- `process.run` isolé avec Bubblewrap sans réseau, capacités ni environnement
  hôte, soumis à des limites POSIX de CPU, mémoire, processus, fichiers et
  descripteurs, limité aux arguments/commandes autorisés, et associé à des accès
  fichiers par descripteurs résistants aux échanges de symlinks. Après
  redémarrage, une exécution déjà revendiquée devient `failed` avec résultat
  incertain. Un appel approuvé mais encore antérieur à la revendication devient
  `failed` comme non démarré. Aucun des deux n'est rejoué automatiquement.
  Les alias par lien physique d'un chemin protégé sont aussi refusés. Si
  `/usr/bin/bwrap` ou le limiteur `/usr/bin/prlimit` manque, l'exécution est
  refusée sans fallback direct.

Le contrat complet est dans [`api/openapi.yaml`](api/openapi.yaml) et la procédure
locale dans [`docs/18-dev-setup.md`](docs/18-dev-setup.md).

## Runtime de buts v0.12

Le [développement de projets v0.14](docs/32-project-coding.md) ajoute une
conversation persistante, les réponses aux clarifications, des fichiers versionnés,
des vérifications Python/Node isolées et la poursuite du même projet après une
nouvelle demande. Les révisions se publient après une approbation liée à leur contenu.

### IMPLEMENTED

- Une ressource `goal` distincte des tâches ordinaires porte l'objectif,
  le profil d'autonomie, les critères de fin et des budgets persistés de pas,
  parallélisme, replans, durée et appels modèle. Créer un but ne le démarre pas:
  `start`, `replan` et `cancel` restent des commandes authentifiées explicites.
- Le planner Ubuntu ou une proposition `iphone_local`/`manual` produit seulement
  un `SwarmPlanProposal` JSON strict. Le serveur refuse les champs d'état ou de
  succès, les cycles, dépendances inconnues, skills non autorisés, dépassements
  de budget et changements de l'objectif autoritatif avant de persister le DAG.
- Chaque nœud worker crée sa propre tâche enfant et passe par le dispatcher,
  le scheduler, les leases, la politique de skill et les gateways existants.
  Les nœuds indépendants peuvent progresser jusqu'au parallélisme autorisé;
  aucun modèle ne choisit l'éligibilité ni ne contourne une approbation.
- Un évaluateur Ubuntu séparé reçoit une projection bornée des résultats. Sa
  décision reste une proposition strictement validée (`continue`, `replan`,
  `done`, `failed`, `needs_user`). La fin exige des preuves worker observées par
  le serveur et conformes au contrat du skill. Verdict, fingerprint, fin du
  model-call et transition sont atomiques; les décisions répétées sans
  changement d'état et les plans équivalents sont stoppés.
- Le `ModelRouter` immuable choisit des IDs de modèle configurables et distincts
  pour le planner et l'évaluateur, sans posséder lui-même de fonction
  d'inférence ou d'exécution.
- Le résultat final est reconstruit depuis les résumés SQLite autoritatifs,
  expurgé et accompagné de provenance; les objets worker bruts ne sont jamais
  copiés dans la réponse mobile.
- `ContextBuilder` produit des cartes déterministes, expurgées et bornées avec
  provenance pour objectif, nœud, dépendances, contraintes, budgets, échecs,
  mémoire, épisodes et agents. Les limites mémoire/épisodes/agents/upstream et
  la taille de chaque résultat sont indépendantes du budget global de tokens;
  le JSON compté/persisté est exactement celui transmis au planner/evaluator.
- Une trajectoire terminale crée un épisode résumé et ses étapes. La recherche
  combine pertinence lexicale ou sémantique optionnelle, skill, outcome,
  récence et feedback utilisateur. `StrategyRetrieval` ne renvoie que de courts
  enseignements de succès/échec/mémoire, jamais un ancien plan exécutable.
- Le feedback de but alimente quatre exports JSONL expurgés (`planner`,
  `evaluator`, `synthesis`, `routing`). Un exemple n'est marqué candidat au
  fine-tuning que s'il est terminé, revu, suffisamment bien noté et possède une
  correction humaine adaptée.
- L'app Expo ajoute une vue Swarm et un détail de but pour créer, démarrer,
  relancer, annuler, suivre les nœuds/preuves et noter un résultat. La réplica
  locale conserve buts, nœuds et résultats par origine, mais reste en lecture
  seule hors ligne pour toutes ces commandes.

Voir [`docs/28-autonomous-swarm-runtime.md`](docs/28-autonomous-swarm-runtime.md),
[`docs/29-context-engineering.md`](docs/29-context-engineering.md),
[`docs/30-episodic-memory.md`](docs/30-episodic-memory.md) et
[`docs/31-swarm-evaluation.md`](docs/31-swarm-evaluation.md).

### EXPERIMENTAL / PLANNED

- Le routage dynamique/failover n'est pas implémenté. Les providers planner et
  evaluator utilisent encore le même endpoint OpenAI-compatible; les routes
  `summarizer`/`synthesizer` restent des métadonnées sans provider actif et la
  synthèse v0.12 est déterministe.
- La recherche d'épisodes est interne au planner; elle n'a pas encore d'API ou
  d'écran de gestion. FAISS ne projette pas encore les épisodes; la provenance
  publique ne contient que les IDs des sources réellement retenues au contexte.
- Aucun entraînement, changement de prompt ou déploiement de modèle ne se fait
  automatiquement depuis le feedback.

## Qualification et frontière de déploiement v0.11

### QUALIFIED

- Les pertes de lease de maintenance et les publications qui dépassent leur
  lease ont des tests de fencing/récupération; l'outbox converge par livraison
  au moins une fois avec déduplication côté transport.
- Un Redis authentifié réel a été exécuté sur `ubuntu-host` derrière un tunnel
  SSH: 8 tests ont couvert authentification, panne/reprise, déduplication,
  timeout et rétention. TLS Redis et le rejet d'un certificat invalide restent
  non qualifiés.
- Le smoke test multi-worker couvre une source de vérité SQLite unique, deux
  identités worker authentifiées, le transfert d'une job de lecture après
  expiration et le fencing du worker ancien. Il valide le protocole dans des
  identités isolées; il ne lance pas les workers sur deux machines physiques.
- Les niveaux sont séparés: `scripts/check.sh` pour l'unitaire,
  `scripts/check-integration.sh` pour les contrats externes et le smoke test,
  `scripts/check-chaos.sh` pour les pannes bornées, et le protocole iPhone pour
  la validation physique manuelle.

### SUPPORTED

- Un control plane Ubuntu autoritatif sur son SQLite local.
- Plusieurs workers distants qui utilisent exclusivement l'API worker
  authentifiée.
- Redis Streams optionnel comme fabric de notification reconstruisible.

### EXPERIMENTAL / UNSUPPORTED

- Plusieurs control planes écrivant le même SQLite sur NFS ou un filesystem
  réseau, et tout mode active-active SQLite inter-hôtes, sont non supportés.
- Le harness multi-worker est une qualification de protocole; le placement sur
  plusieurs hôtes physiques doit encore produire sa propre preuve de
  déploiement.
- TLS Redis, certificat invalide et consumers Redis opérationnels restent à
  qualifier avant une revendication de production multi-hôte complète.

### MANUAL VALIDATION REQUIRED

- Aucun iPhone physique n'était disponible dans l'hôte QEMU. Les six
  capabilities (location, contacts, calendrier, photo, mail et SMS), leurs
  refus/annulations, arrière-plan, expiration, perte réseau et absence de rejeu
  restent `NOT RUN` selon
  [`docs/26-iphone-physical-device-validation.md`](docs/26-iphone-physical-device-validation.md).
  La tentative bloquée et ses diagnostics expurgés sont consignés dans
  [`docs/evidence/iphone-validation-2026-09-08.md`](docs/evidence/iphone-validation-2026-09-08.md).

### IMPLEMENTED — npm audit triage

- `npm audit` signale toujours 13 avis modérés transitifs. Aucun `--force`
  incompatible avec Expo SDK 57 n'a été appliqué; la disposition détaillée est
  dans [`docs/security/npm-audit-v011.md`](docs/security/npm-audit-v011.md).

## Structure du dépôt

```text
mongars-swarm/
  README.md
  MASTER_SPEC.md
  docs/                Spécifications fonctionnelles et techniques
  adrs/                Architecture Decision Records
  api/                 Contrat OpenAPI de l'API locale authentifiée
  configs/             Configs de départ YAML
  schemas/             JSON Schemas de protocole
  prompts/             Prompts système par rôle
  diagrams/            Diagrammes Mermaid
  server/tests/        Tests exécutables du control plane
  tests/               Plans de tests Expo, RN, backend et QA emulator
  checklists/          Checklists build, review et release
```

## Ordre de lecture recommandé

1. `MASTER_SPEC.md`
2. `docs/01-product-vision.md`
3. `docs/03-system-architecture.md`
4. `docs/04-mobile-expo-architecture.md`
5. `docs/05-ubuntu-control-plane.md`
6. `docs/08-state-memory-sync.md`
7. `docs/09-permission-gateway.md`
8. `docs/15-testing-strategy.md`
9. `docs/16-code-analysis-quality-gates.md`
10. `docs/23-implementation-plan.md`
11. `docs/27-production-qualification.md`
12. `docs/28-autonomous-swarm-runtime.md`
13. `docs/29-context-engineering.md`
14. `docs/30-episodic-memory.md`
15. `docs/31-swarm-evaluation.md`

## Mode de build visé

La console sans runtime natif peut encore démarrer avec Expo Go. Le module
d'inférence locale exige désormais un **development build** Expo; Expo Go ne
contient pas les binaires Core ML, MLX et llama.cpp de l'application.

## Stack cible

Mobile:

- Expo + React Native + TypeScript
- Expo Router
- SQLite local côté iPhone
- TanStack Query ou cache maison pour lecture/réconciliation
- WebSocket sécurisé avec Ubuntu
- SecureStore pour secrets courts
- modules Expo officiels pour caméra, location, contacts, calendrier, photos, audio
- development client dès qu'on ajoute MLX/llama.cpp/Core ML ou un bridge Swift custom

Ubuntu — présent dans le MVP:

- FastAPI control plane
- llama.cpp/Ollama/vLLM-compatible OpenAI local endpoint
- SQLite WAL autoritatif; Postgres n'est pas branché
- board SQLite par défaut; Redis Streams optionnel comme transport de
  notification, jamais comme source de vérité
- consumer ledger SQLite pour services de confiance du control plane
- projection FAISS locale optionnelle et reconstruisible depuis SQLite
- workers de lecture Files, Research borné et Code Review en lecture seule
- append-only audit log
- pytest + ruff + mypy + bandit

Ubuntu — évolutions ciblées, non annoncées comme déjà livrées:

- qualification TLS Redis, orchestration de consumer groups Redis et
  procédures de reprise/monitoring en production
- déploiement prouvé de workers sur plusieurs machines physiques
- NATS JetStream, si une migration future le justifie
- migration Postgres si plusieurs writers deviennent nécessaires
- branchement de la projection FAISS dans le chemin de recherche en production
  ou adaptateur Qdrant; le MVP garde SQLite et le fallback lexical

## Non-objectifs du MVP

- Pas d'autonomie cachée.
- Pas d'envoi SMS/iMessage automatique sans UI utilisateur.
- Pas de contournement des permissions iOS.
- Pas d'auto-entraînement live sans revue, versioning et rollback.
- La gate CI unitaire est obligatoire; les mêmes checks restent exécutables localement.
