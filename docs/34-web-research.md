# Recherche Internet locale

## Évaluateur de recherche optionnel

`MONGARS_RESEARCH_EVALUATOR_MODEL` permet de choisir explicitement un évaluateur
pour les plans composés uniquement de workers `research.query`, `writing.draft`
et de nœuds de synthèse. Il faut aussi qu'un résultat de recherche terminé et
non vide soit encore présent dans le contexte borné envoyé au modèle. Un plan
mixte contenant du code, une lecture de workspace ou des métadonnées anciennes
incomplètes conserve l'évaluateur habituel, même si ces nœuds ont été omis du
contexte pour respecter son budget.

Cette option est désactivée par défaut; une valeur vide équivaut à son absence.
Elle ne modifie ni `MONGARS_EVALUATOR_MODEL`, ni le planificateur, ni les modèles
des workers. L'évaluateur optionnel utilise le même endpoint, les mêmes règles
de validation et le même délai absolu, avec `reasoning_effort: "none"` dans sa
requête. Le fournisseur habituel continue d'omettre ce champ. Le modèle
effectivement choisi est inscrit dans la réservation durable de l'appel avant
son exécution, avec les limites et les mécanismes de contrôle existants.

Configurer ce champ nécessite de qualifier le modèle et sa prise en charge de
`reasoning_effort` sur les réponses pertinentes comme sur les preuves hors
sujet. L'existence du réglage et la réussite des tests ne constituent pas une
qualification du modèle ni une activation en production.

Le planificateur Ubuntu expose séparément
`MONGARS_PLANNER_REASONING_EFFORT=none`. Le réglage est absent par défaut; une
valeur vide laisse aussi le champ hors de la requête. Seule la valeur `none`
est acceptée lorsqu'il est configuré. Ce réglage ne change ni le modèle choisi,
ni son délai, ni les validations du plan. Le rédacteur conserve son transport
Ollama natif et son comportement existant `think: false` pour les identifiants
contenant `qwen3`; aucun nouveau réglage de worker n'est nécessaire.

## Déploiement de la passerelle SearXNG

La pile historique `/home/ales27pm/original-monGARS` héberge déjà SearXNG et son
proxy de sortie Squid. Swarmer réutilise ce moteur sans recréer ses conteneurs,
modifier sa configuration ou publier directement son port. Le conteneur
`swarmer-searxng-gateway`, projet Compose `swarmer-search-bridge`, rejoint
le réseau interne existant `mongars_search` et un bridge ingress dédié au projet et transmet les requêtes à l’alias
Docker fixe `searxng:8080`.

L’endpoint destiné au worker est `http://127.0.0.1:8721/search`. Seul `POST /search`
est accepté. Le corps est un formulaire comprenant `q` et `format=json`. La
passerelle retire les en-têtes Authorization, Proxy-Authorization et Cookie,
limite le corps à 32 Kio (32 768 octets) et désactive la journalisation des requêtes. Elle n’expose
pas d’administration, de configuration, ni d’URL amont choisie par le client.
Les titres, URL et extraits retournés restent des données externes non fiables.

### Prérequis et isolation

La configuration est dans `configs/searxng`. Le fichier Compose épingle le
binaire Caddy local v2.11.4 par son identifiant d’image immuable; il interdit le
téléchargement implicite. Sur Ubuntu, Docker système 29.8 et Compose 5.5.1 sont
accessibles au compte opérateur. Ce daemon est privilégié; il ne s’agit pas de
Docker rootless et son socket n’est jamais monté dans la passerelle.

Le conteneur tourne en UID/GID 65534, avec racine en lecture seule, aucune
capability, `no-new-privileges`, 128 Mio de mémoire, 32 processus et 0,25 CPU. Son seul
port publié est lié à 127.0.0.1. Un `/tmp` privé et borné sert aux données
temporaires. Le bridge ingress permet la publication du port sous Docker 29 et offre une
route sortante potentielle à Caddy. Ce réseau ne constitue pas une barrière de
sortie au niveau du système : le protocole et l’amont fixes de Caddy bornent
l’accès fonctionnel. SearXNG conserve ses réseaux et son proxy de sortie.

SearXNG utilise l’image existante
`sha256:b8ca38ba06eea544d7555e88321e212ddc0d5c3c7de055419cfb2e5c6bf30812`.
Il autorise uniquement les résultats JSON et utilise le proxy fixe
`search-egress-proxy:3128`; les délais moteur sont de 5 à 10 secondes. Dans la pile SearXNG historique, Squid est le
seul composant relié au réseau de sortie. Ses ACL refusent notamment les réseaux
privés, loopback, link-local et les endpoints de métadonnées IPv4/IPv6, et limitent
les ports à 80/443. Ces composants existants ne sont pas gérés par le Compose
Swarmer. Leurs consignes restent dans
`original-monGARS/deploy/searxng/README.md` et
`original-monGARS/deploy/egress-proxy/README.md`.

### Installation opérateur

1. Vérifier que `mongars-searxng-1` et son proxy sont sains, que le réseau
   `mongars_search` est interne et que le port 8721 ainsi que le nom
   `swarmer-searxng-gateway` sont libres. Un conteneur du même nom doit être
   inspecté avant remplacement.
2. Copier uniquement `Caddyfile` et `compose.yaml` dans une release immuable sous
   `~/.config/swarmer-research-gateway/releases/`, puis vérifier leurs SHA256.
3. Valider Compose sans afficher de variables privées :
   `docker compose --project-directory CHEMIN_RELEASE config --quiet`.
4. Valider le Caddyfile avec l’image épinglée dans un conteneur jetable sans
   réseau, avant toute publication de port.
5. Démarrer uniquement la nouvelle passerelle :
   `docker compose --project-directory CHEMIN_RELEASE up -d --no-deps --pull never gateway`.
6. Vérifier les limites effectives, le binding loopback, les deux réseaux attendus et les
   identifiants/dates de démarrage inchangés des conteneurs historiques.
7. Exécuter `python3 configs/searxng/verify_gateway.py` pour une seule recherche
   publique non sensible et deux contrôles de routes. Le script n’affiche que
   les compteurs et au maximum trois URL. Il n’exécute aucun modèle.

La passerelle n’inscrit pas d’agent, ne démarre pas de but et ne modifie pas la
configuration de l’API. L’inscription du worker de recherche et la qualification
de son contrat restent des étapes distinctes. Ne pas déclarer le parcours
assistant→recherche→réponse validé sur la seule base du test de cette passerelle.

Pour retirer la passerelle, utiliser `docker compose --project-directory
CHEMIN_RELEASE down` sur ce projet dédié seulement. Le réseau déclaré externe
et la pile historique sont conservés.

Références officielles : [API de recherche SearXNG](https://docs.searxng.org/dev/search_api.html)
et [installation Docker](https://docs.searxng.org/admin/installation-docker.html).
