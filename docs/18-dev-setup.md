# 18 — Development Setup

Cette procédure lance le MVP local authentifié. Elle ne dépend pas de la couche
sans authentification, des statuts simulés ou du branchement de base de données du
prototype Vibecode.

## Prérequis

- Ubuntu pour l'exécution de processus isolés;
- Python 3.12 ou plus récent;
- Bubblewrap disponible exactement à `/usr/bin/bwrap`;
- Node.js conforme au champ `engines` de `mobile/package.json` (actuellement
  Node 22.13+ ou 24+);
- un endpoint local compatible OpenAI pour la planification, par défaut
  `http://127.0.0.1:8711/v1`.

Sur Ubuntu:

```bash
sudo apt update
sudo apt install bubblewrap python3 python3-venv
python3 --version
/usr/bin/bwrap --version
```

Bubblewrap est obligatoire uniquement pour `process.run`, mais son absence ne
dégrade jamais cette action en exécution directe: l'appel échoue fermé.

## Structure utile

```text
mongars-swarm/
  mobile/                  Application Expo
  server/                  Control plane FastAPI et tests
  api/openapi.yaml         Contrat de l'API authentifiée
  configs/permissions.yaml Politique d'outils et de sandbox
  data/                    État local ignoré, selon la configuration
  docs/                    Architecture et opérations
```

## Installer le backend

Depuis la racine du dépôt:

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
```

Le backend lit un fichier `.env` dans son répertoire de travail. Les noms de
variables actuels correspondent directement à `server/app/settings.py`:

```dotenv
MONGARS_ENV=dev
MONGARS_HOST=0.0.0.0
MONGARS_PORT=8710
MONGARS_DB_PATH=./data/mongars.db
MONGARS_WORKSPACE_ROOT=./workspace
MONGARS_REDIS_URL=redis://127.0.0.1:6379/0
MONGARS_LLM_BASE_URL=http://127.0.0.1:8711/v1
MONGARS_ORCHESTRATOR_MODEL=Hermes-3-Llama-3.2-3B-abliterated
MONGARS_PERMISSIONS_PATH=../configs/permissions.yaml
MONGARS_PAIRING_BOOTSTRAP_TOKEN=replace-with-a-long-random-operator-token
MONGARS_PAIRING_CODE_TTL_SECONDS=600
MONGARS_PAIRING_MAX_ATTEMPTS=10
MONGARS_ALLOW_INSECURE_REMOTE_HTTP=false
```

Créer la valeur `MONGARS_PAIRING_BOOTSTRAP_TOKEN` avec un générateur aléatoire,
par exemple `openssl rand -hex 32`, puis la placer uniquement dans `server/.env`.
Ne pas la envoyer au téléphone, la mettre dans Git, l'imprimer dans les logs ou
la confondre avec le jeton d'appareil.

La durée d'un code est bornée par le serveur entre 60 et 900 secondes et le
budget de tentatives entre 1 et 20, même si la configuration demande davantage.

## Endpoint du modèle local

Le planificateur appelle un endpoint compatible OpenAI à
`MONGARS_LLM_BASE_URL`. Avec llama.cpp, un lancement générique ressemble à:

```bash
llama-server \
  -hf mradermacher/Hermes-3-Llama-3.2-3B-abliterated-GGUF:Q4_K_M \
  --host 127.0.0.1 \
  --port 8711 \
  -c 8192
```

Le modèle propose un outil et ses arguments. Il n'exécute rien directement et
n'accorde aucune permission. Une proposition `tool_name: none` laisse la tâche
dans l'état `planned`; son texte est étiqueté `proposal_only`, pas `completed`.

## Lancer le control plane

Depuis `server/`, avec l'environnement virtuel actif:

```bash
mkdir -p data workspace
uvicorn app.main:app --reload --host 0.0.0.0 --port 8710
```

Vérifier la route publique de diagnostic:

```bash
curl --fail-with-body http://127.0.0.1:8710/health
```

`/health` ne donne accès à aucune ressource. Toutes les API de tâches, chat,
approbations, mémoire, agents, audit, feedback, bootstrap et tickets WebSocket
exigent un jeton d'appareil valide.

## Appairage sans self-pairing distant

L'émission du code exige deux conditions simultanées:

1. la connexion TCP vue par FastAPI vient réellement d'une adresse loopback;
2. l'en-tête `X-Mongars-Operator-Token` correspond au secret bootstrap.

Les en-têtes de proxy comme `X-Forwarded-For` ne satisfont pas la première
condition. Depuis la machine Ubuntu elle-même, ou dans une session SSH vers elle:

```bash
read -rs MONGARS_OPERATOR_TOKEN
curl --fail-with-body \
  -X POST \
  -H "X-Mongars-Operator-Token: ${MONGARS_OPERATOR_TOKEN}" \
  http://127.0.0.1:8710/pairing/code
unset MONGARS_OPERATOR_TOKEN
```

Saisir la valeur du secret quand `read` attend l'entrée. La réponse contient un
code à six chiffres et `expires_in_seconds`. Le serveur ne conserve qu'un HMAC
du code, lié au secret opérateur,
n'autorise qu'un code actif et le consomme à la première réussite.

L'application mobile envoie ensuite à `/pairing/complete`:

```json
{
  "code": "123456",
  "device_id": "iphone-user-1",
  "name": "iPhone"
}
```

Cette route d'échange est accessible sans jeton parce que le nouvel appareil n'en
a pas encore, mais le serveur refuse HTTP en clair dès que le pair réseau n'est
pas loopback. Sa protection repose sur TLS, le code court expirant, le budget de
tentatives et la consommation atomique. La réponse contient un candidat court
(`candidate_token`, `pairing_id`, `device_id`, `expires_in_seconds`) dont le
serveur ne conserve que le digest. Avant finalisation, ce bearer peut uniquement
lire `/sync/bootstrap` et appeler `/pairing/finalize` avec les mêmes identifiants.
La finalisation est idempotente et marque le candidat `ready`; elle ne révoque pas
le jeton actif.

Le client mobile refuse lui-même HTTP avant tout envoi hors loopback. Après avoir
vérifié le bootstrap et finalisé la liaison, il écrit une connexion de reprise
dans une clé SecureStore séparée, puis relit `/sync/bootstrap` avec le candidat.
Cette première authentification ordinaire active atomiquement le nouveau bearer,
révoque l'ancien et invalide les tickets WebSocket inutilisés. La reprise reste
durable si la réponse est perdue ou si l'écriture de la connexion active échoue;
un candidat expiré reçoit `401` et le client revient à la connexion active encore
stockée. L'ancien bearer n'est jamais envoyé au nouveau serveur.
Une installation mise à niveau qui ne possède que les anciennes clés URL et
jeton séparées échoue fermée et demande un nouvel appairage; elle ne tente pas de
deviner leur association.

Pour un smoke test en terminal, saisir le jeton sans l'ajouter à l'historique:

```bash
read -rs MONGARS_DEVICE_TOKEN
curl --fail-with-body \
  -H "Authorization: Bearer ${MONGARS_DEVICE_TOKEN}" \
  https://ubuntu.example/sync/bootstrap
unset MONGARS_DEVICE_TOKEN
```

## Cycle réel d'une tâche

- `POST /chat` persiste un message utilisateur et une tâche `created`;
- `POST /tasks/{task_id}/plan` demande une proposition au modèle local;
- un outil de lecture validé peut produire immédiatement un résultat réel;
- `workspace.write_text` et `process.run` créent atomiquement leur appel et une
  approbation `pending`; aucune route ne permet de créer une approbation orpheline;
- cette approbation stocke l'identifiant de l'appel et une empreinte canonique de
  son outil et de tous ses arguments. La décision et la revendication d'exécution
  recalculent cette liaison avant d'agir;
- `POST /approvals/{approval_id}/decision` accepte `allow_once`, `approve` ou
  `deny`;
- une décision réussit une seule fois. Un replay, une décision concurrente ou
  une approbation expirée reçoit `409`;
- l'annulation d'une tâche invalide atomiquement ses approbations en attente et
  ses appels d'outils non exécutés; les états annulés et terminaux ne peuvent pas
  être ressuscités par une nouvelle proposition;
- seul le retour réussi de l'exécuteur place la tâche dans `completed`.
- après un redémarrage, une revendication restée `running` devient `failed` avec
  résultat explicitement incertain. Un appel sensible approuvé mais encore
  `queued` avant sa revendication devient `failed` comme non démarré. Aucun des
  deux n'est relancé automatiquement.

Il n'existe pas de décision `allow_rule` dans le MVP. `approve` est un alias API
de l'autorisation ponctuelle, pas une règle persistante.

## Isolation de `process.run`

`configs/permissions.yaml` est la source de la politique d'exécution. Avant de
créer un appel de processus, le serveur vérifie notamment:

- une liste `argv` structurée, de taille bornée, sans interpréteur shell;
- le nom de commande dans `execution.allowed_commands`;
- les sous-commandes npm/npx et les options d'évaluation inline interdites;
- un `cwd` existant à l'intérieur de `MONGARS_WORKSPACE_ROOT`;
- la présence exécutable du binaire configuré, par défaut `/usr/bin/bwrap`.

Après l'approbation ponctuelle, Bubblewrap démarre avec des namespaces isolés,
sans réseau, sans capacités, sans environnement hôte, avec des `/tmp` et `/run`
privés. Des limites POSIX bornent aussi CPU, mémoire, nombre de processus, taille
de fichier et descripteurs ouverts. Les répertoires système utiles sont en lecture
seule, seul le workspace est monté en écriture, et les chemins protégés (`.env`,
clés, tokens, `.git/config`, `.npmrc`, etc.) sont masqués. Le temps et les sorties
sont bornés par la politique. Les lectures et écritures directes traversent le
workspace par des descripteurs sans suivre les symlinks; les lectures sont bornées
avant allocation et les écritures sont installées par remplacement atomique.
Une lecture/écriture directe compare aussi l'inode cible aux fichiers protégés.
Un processus est refusé avant Bubblewrap si un fichier protégé possède plusieurs
liens physiques, ce qui ferme les alias comme `notes.txt -> .env`.

Sur macOS, où ce backend Bubblewrap n'est normalement pas disponible, les tests
peuvent valider la construction de la commande mais une exécution `process.run`
doit être considérée non qualifiée et refusée. Valider l'isolation réelle sur
Ubuntu avant de déclarer ce chemin prêt en production.

## WebSocket authentifié

Un client déjà appairé demande d'abord:

```http
POST /ws/ticket
Authorization: Bearer <device-token>
```

La réponse contient un ticket valable 30 secondes. Ouvrir ensuite
`wss://ubuntu.example/ws?ticket=<ticket>`. Le ticket est supprimé à la première
tentative de consommation, réussie ou non, et ne peut pas être rejoué. Le jeton
d'appareil longue durée ne doit jamais être placé dans l'URL WebSocket.

Les événements couvrent actuellement les transitions de tâche, propositions et
résultats d'outils, demandes/décisions d'approbation et propositions du modèle.

## Surface API actuelle

Le contrat détaillé est [`api/openapi.yaml`](../api/openapi.yaml). Il couvre:

- tâches enrichies, détails, annulation, planification et appels d'outils;
- chat, conversations et messages;
- approbations ponctuelles non rejouables;
- mémoire CRUD et recherche lexicale explicitement étiquetée;
- registre d'agents: un agent démarre non vérifié et reçoit une credential dédiée,
  retournée une seule fois, pour authentifier ses heartbeats;
- bootstrap de réplica, audit chaîné par hash et feedback;
- émission de tickets WebSocket à usage unique.

Les alias de compatibilité `/memory/remember`, `/sync/audit` et `/sync/feedback`
existent encore mais sont masqués du schéma généré. Utiliser les routes canoniques
`/memory`, `/audit` et `/feedback` dans tout nouveau client.

## Installer et lancer l'application Expo

```bash
cd mobile
npm install
npm run typecheck
npm run lint
npm test
npx expo start
```

Configurer l'URL du serveur privé dans l'écran Settings puis saisir le code émis
par l'opérateur. L'adresse n'est pas enregistrée séparément: une réussite lie
atomiquement l'origine et le jeton dans SecureStore. Les données répliquées non
secrètes restent dans SQLite.

Créer un development build seulement lorsqu'un module natif custom l'exige:

```bash
npx expo install expo-dev-client
eas build -p ios --profile development
npx expo start --dev-client
```

## Réseau local

Le listener `0.0.0.0` permet à l'iPhone appairé de joindre Ubuntu, mais le serveur
refuse par défaut les échanges HTTP et WebSocket en clair dont le pair n'est pas
loopback. Exposer le service derrière une terminaison TLS locale (par exemple sur
le réseau privé Tailscale) et limiter le port 8710 au firewall. La variable
`MONGARS_ALLOW_INSECURE_REMOTE_HTTP=true` est une dérogation explicite de
développement et ne convient pas à un réseau observable.

## Vérification locale

Backend:

```bash
cd server
.venv/bin/pytest tests -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy app
.venv/bin/bandit -r app
```

Mobile:

```bash
cd mobile
npm run typecheck
npm run lint
npm test
npx --no-install expo-doctor
```

Ces vérifications prouvent les couches source, typage et tests. Elles ne prouvent
pas à elles seules l'exécution Bubblewrap sur Ubuntu, le comportement d'un modèle
live, une installation iPhone signée ou un déploiement.
