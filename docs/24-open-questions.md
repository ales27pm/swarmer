# 24 — Open Questions

## Product

- Nom final: monGARS, monGARS Swarm, 27PM Agent Console?
- iPhone seulement ou Android support minimal aussi?
- Interface vocale dès MVP ou après task/approval?

## Models

- llama.cpp ou Ollama comme serveur initial?
- Hermes abliterated seul ou G9v3 en worker parallèle?
- Tester Plano comme benchmark offline?
- Embeddings e5-small ou BGE-M3 dès le départ?

## Infrastructure

- Redis Streams ou NATS directement?
- SQLite combien de temps avant Postgres?
- Qdrant nécessaire dès que memory > 10k chunks?
- Tailscale comme réseau de base?

## iPhone native

- Quelles capabilities prioriser: location/calendar/photos/contacts?
- Development build dès le début ou seulement après MVP Expo Go?
- Besoin de module Swift custom pour SMS composer / EventKit reminders?

## Security

- Quels dossiers Ubuntu autorisés par défaut?
- Quelle politique pour `.env` et secrets?
- Quelles actions peuvent être préapprouvées?
- Audit hash chain obligatoire dès MVP?

## Feedback/training

- Quel seuil avant premier LoRA?
- Quels champs redacter automatiquement?
- Où stocker datasets versionnés?
- Quel outil de training local utiliser?
