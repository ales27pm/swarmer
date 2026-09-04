# Expo Builder Prompt

Build the mobile app for monGARS Swarm App.

Stack:

- Expo + React Native + TypeScript
- Expo Router
- expo-sqlite
- expo-secure-store
- WebSocket + fetch API client
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
app/approval/[id].tsx
```

Features for first slice:

- Settings screen lets user set server URL and pairing code.
- Chat screen sends message to `/tasks`.
- Tasks screen shows local replica status.
- Approvals screen shows pending approvals from sync/WebSocket.
- SQLite stores messages/tasks/approvals/sync_outbox.
- SecureStore stores device token.
- Offline messages enter outbox.
- WebSocket reconnect updates local replica.

Testing:

- render Chat screen;
- send message calls API client;
- pending approval card renders action/risk/target;
- allow once posts decision;
- offline message stored in outbox.
