# 04 — Mobile Expo Architecture

## Objectif

Construire une app iPhone Expo/React Native qui agit comme console locale-first du swarm.

## État du document

Les sections « capacités natives » restent une cible d'architecture. Le slice
`0.6` implémente la console authentifiée, un cache SQLite hydraté au bootstrap et les écrans
décrits ci-dessous; il ne revendique ni streaming, ni outbox offline, ni accès
aux capteurs, ni souscription live mobile tant que ces flux ne sont pas exercés.

## Navigation proposée

Expo Router avec groupes:

```text
app/
  _layout.tsx
  (main)/
    _layout.tsx
    index.tsx              Chat / command center
    tasks.tsx              Task timeline
    approvals.tsx          Permission approvals
    memory.tsx             Memory search / pinned context
    agents.tsx             Agent registry visible
    settings.tsx           Pairing, server, permissions
  task/[id].tsx            Task detail
  agent/[id].tsx           Agent detail
  approval/[id].tsx        Approval detail
```

Règle: ne pas mettre components/types/utils dans `app/`. Garder `app/` pour les routes.

```text
src/
  components/
  features/
    chat/
    tasks/
    approvals/
    memory/
    agents/
    settings/
    native-bridge/
    sync/
  lib/
    api/
    db/
    auth/
    ws/
    schemas/
  stores/
  types/
```

## Écrans MVP

### Chat

- input texte;
- état connexion;
- étapes explicites de création et planification;
- lien vers tâche et preuves réelles;
- aucune minuterie ou réponse de réussite simulée.

### Tasks

- liste des tâches;
- filtres pour tous les états du cycle de vie;
- timeline simplifiée.

### Approvals

- demandes de permissions;
- carte partagée entre la file et le détail de tâche;
- cible ou commande expurgée, identifiant d'appel et empreinte canonique;
- texte libre du modèle masqué pour les appels d'outils et remplacé par un
  libellé serveur fixe;
- risques;
- bouton `Autoriser une fois`;
- bouton `Refuser`;
- résultat distinct de la décision et lien persistant vers les preuves;
- liaison invalide non autorisable;
- aucune règle permanente dans le MVP.

### Memory

- recherche lexicale explicitement étiquetée;
- mémoires épinglées;
- création, épinglage et suppression confirmée.

### Agents

- états en ligne, occupé, hors ligne et non vérifié;
- skills;
- modèle;
- dernier heartbeat;
- vue en lecture seule.

### Settings

- code à six chiffres émis hors de l'app par l'opérateur local;
- origine authentifiée affichée séparément de toute nouvelle adresse candidate;
- succès seulement après bootstrap candidat, finalisation liée, stockage de
  reprise et bootstrap de bascule authentifié;
- ancien stockage URL/jeton séparé refusé jusqu'à un nouvel appairage;
- extrait du journal d'audit.

## Local storage iPhone

SQLite local implémenté:

- `tasks`
- `approvals`
- `sync_meta`

Ce stockage est actuellement un cache write-through du bootstrap. Les écrans
lisent les réponses REST authentifiées et utilisent un rafraîchissement manuel;
aucun mode offline, replay ou source UI locale n'est revendiqué.

SecureStore:

- connexion active liant URL normalisée et jeton opaque d'appareil;
- candidat de bascule lié à son `pairing_id` et `device_id`, conservé séparément
  jusqu'à confirmation de l'activation;
- identifiant local de l'appareil.

Le client accepte HTTP uniquement pour les hôtes loopback. Il valide une nouvelle
origine et échange son code sans ancien bearer. Le candidat ne peut que lire le
bootstrap et se finaliser. Après finalisation, le client l'enregistre comme reprise
avant le bootstrap qui déclenche la bascule serveur. Une réponse perdue reprend ce
candidat; un `401` candidat restaure la connexion active conservée. Aucun ancien
bearer n'est envoyé au serveur candidat.

## Sync

- REST pour bootstrap dans le client actuel.
- Ticket WebSocket à usage unique disponible côté API; reconnexion et
  souscription mobile restent à implémenter et tester.
- Pas d'outbox offline revendiquée dans le slice actuel.
- Server-authoritative pour permissions, task status, agent state.

## Native bridge Expo

Capacités roadmap avec modules Expo, non incluses dans le slice actuel:

- `expo-location`
- `expo-contacts`
- `expo-calendar`
- `expo-image-picker`
- `expo-media-library`
- `expo-camera`
- `expo-audio`
- `expo-notifications`

Pour appels/SMS/email:

- deep links `tel:` / `mailto:`;
- compose sheet natif via module custom si nécessaire;
- confirmation humaine imposée par iOS pour certaines actions.

## Expo Go vs Development Build

Phase 1 doit tenter Expo Go pour itérer vite.

Development build requis dès que:

- module Swift custom pour iPhone Capability Broker;
- MLX/Core ML/llama.cpp local;
- APIs natives non couvertes par Expo Go;
- config native avancée;
- extension/app target Apple.

## Data fetching

Préférence:

- `fetch` natif / `expo/fetch`;
- éviter axios;
- wrapper `apiClient` unique;
- erreurs typées;
- retry/backoff;
- React Query si le cache devient lourd.

## UX permission

Une demande d'approbation doit montrer:

- le demandeur authentifié, dérivé du principal serveur;
- l'action exacte liée à l'appel;
- le motif et l'identifiant de la règle de politique exécutable;
- un résumé expurgé des données touchées, dérivé par le serveur;
- risque;
- commande exacte si applicable;
- durée de permission;
- l'identifiant de l'événement d'audit durable.

Ces preuves vérifiées sont rendues dans un bloc distinct du résumé libre du
modèle. Ce dernier reste visible comme contexte non vérifié, mais ne peut ni
fournir l'identité du demandeur, ni justifier la politique, ni autoriser
l'action. L'expiration est aussi calculée localement: à l'échéance, la carte
affiche `Expiré` et ses décisions sont désactivées, même avant le prochain
rafraîchissement serveur.

## Exemple d'approval card

```text
Authenticated requester: Ales iPhone (device iphone-15)
Policy: ask-workspace-write — Writing workspace file content requires explicit one-use approval.
Action: workspace.write_text
Target: src/api/client.ts
Affected data: 412 UTF-8 bytes; content hidden
Risk: medium
Audit: 1842
Server-derived public label: Write text to a workspace file
Model tool details: hidden
Options: Allow once | Deny | View task evidence
```

## Accessibilité

- Texte important `selectable`.
- États d'erreur explicites.
- Gros boutons d'approbation.
- Haptics sur succès/risque.
- Mode sombre.
- Logs copiables.
