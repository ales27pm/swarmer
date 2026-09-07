# Expo Builder Prompt

Build the mobile app for monGARS Swarm App.

Stack:

- Expo + React Native + TypeScript
- Expo Router
- expo-sqlite
- expo-secure-store
- authenticated fetch API client
- React Native Testing Library

Routes:

```text
app/_layout.tsx
app/(main)/_layout.tsx
app/(main)/index.tsx
app/(main)/tasks.tsx
app/(main)/approvals.tsx
app/(main)/memory.tsx
app/(main)/agents.tsx
app/(main)/settings.tsx
app/task/[id].tsx
```

Features for first slice:

- Settings screen lets user set server URL and pairing code.
- Chat screen sends messages to `/chat` and requests real proposals through the task plan route.
- Tasks and approvals screens load canonical REST resources and expose manual refresh.
- `/sync/bootstrap` writes tasks, approvals and the audit cursor to SQLite as a cache.
- SecureStore binds origin and token, with a separate durable pending record for
  staged pairing cutover and lost-response recovery.
- Proposal-only model text must never appear as verified executor completion.
- Do not add offline replay, outbox, or mobile WebSocket claims in this slice.

Testing:

- render Chat screen;
- send message calls API client;
- pending approval card renders action/risk/target;
- allow once posts decision;
- bootstrap cache writes and local approval reads are covered at the SQLite boundary.
