# 04 — Mobile Expo Architecture

## Objectif

Construire une app iPhone Expo/React Native qui agit comme console locale-first du swarm.

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
- bouton micro;
- état connexion;
- streaming réponse;
- chips pour agent utilisé;
- lien vers tâche.

### Tasks

- liste des tâches;
- filtre `running/waiting_permission/failed/completed`;
- timeline simplifiée.

### Approvals

- demandes de permissions;
- détail action;
- risques;
- bouton Allow once;
- bouton Deny;
- bouton Allow rule si safe.

### Memory

- recherche sémantique;
- mémoires épinglées;
- correction/suppression.

### Agents

- online/offline;
- skills;
- modèle;
- endpoint;
- derniers runs.

### Settings

- pairing QR/code;
- adresse Ubuntu;
- permissions iPhone;
- logs;
- export debug.

## Local storage iPhone

SQLite local:

- `conversations`
- `messages`
- `tasks`
- `approvals`
- `sync_outbox`
- `settings`
- `capability_cache`

SecureStore:

- device private token;
- pairing token;
- server fingerprint;
- refresh token court.

## Sync

- REST pour bootstrap.
- WebSocket pour live events.
- Outbox local pour mutations offline.
- Last-write-wins seulement pour champs non critiques.
- Server-authoritative pour permissions, task status, agent state.

## Native bridge Expo

Capabilities initiales avec modules Expo:

- `expo-location`
- `expo-contacts`
- `expo-calendar`
- `expo-image-picker`
- `expo-media-library`
- `expo-camera`
- `expo-audio`
- `expo-notifications`
- `expo-secure-store`
- `expo-sqlite`

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

- qui demande;
- quoi;
- pourquoi;
- données touchées;
- risque;
- commande exacte si applicable;
- durée de permission;
- audit id.

## Exemple d'approval card

```text
Agent: code-worker-01
Action: write_file
Target: /home/alexis/projects/27pm-crm/src/api/client.ts
Reason: appliquer le patch demandé pour corriger la sync
Risk: medium
Options: Allow once | Deny | View diff
```

## Accessibilité

- Texte important `selectable`.
- États d'erreur explicites.
- Gros boutons d'approbation.
- Haptics sur succès/risque.
- Mode sombre.
- Logs copiables.
