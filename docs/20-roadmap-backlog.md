# 20 — Roadmap and Backlog

## Milestone 0 — Docs and repo scaffold

- [ ] Create repo structure.
- [ ] Copy this docs pack.
- [ ] Add configs/schemas/prompts.
- [ ] Add local check script.
- [ ] Decide exact Expo SDK and package versions.

## Milestone 1 — Mobile shell

- [ ] Create Expo app.
- [ ] Add Expo Router routes.
- [ ] Build Chat screen.
- [ ] Build Tasks screen.
- [ ] Build Approvals screen.
- [ ] Build Settings screen.
- [ ] Add SQLite local DB.
- [ ] Add SecureStore token storage.
- [ ] Add API client.
- [ ] Add WebSocket client.

## Milestone 2 — Backend shell

- [ ] FastAPI app.
- [ ] Health endpoint.
- [ ] Pairing endpoints.
- [ ] State Service.
- [ ] SQLite schema.
- [ ] Sync bootstrap/pull/push.
- [ ] WebSocket events.

## Milestone 3 — Orchestrator MVP

- [ ] llama.cpp/Ollama local endpoint.
- [ ] Model manifest loader.
- [ ] Prompt loader.
- [ ] JSON parser.
- [ ] Tool schema validation.
- [ ] Simple task planning.

## Milestone 4 — Gateway MVP

- [ ] Permissions YAML loader.
- [ ] Risk engine.
- [ ] Approval creation.
- [ ] Approval WebSocket push.
- [ ] Allow once flow.
- [ ] Deny flow.
- [ ] Audit log.

## Milestone 5 — First worker

- [ ] Files/code inspect worker.
- [ ] Agent card.
- [ ] Registry heartbeat.
- [ ] Task assignment.
- [ ] Safe read.
- [ ] Patch proposal.
- [ ] Write after approval.

## Milestone 6 — Message board

- [ ] Redis Streams config.
- [ ] Event envelope schema.
- [ ] Consumer groups.
- [ ] Dead letter stream.
- [ ] Task status stream.

## Milestone 7 — Memory Service

- [ ] Embedding model setup.
- [ ] Chunker.
- [ ] FAISS index.
- [ ] Memory search endpoint.
- [ ] Memory write candidates.
- [ ] Memory UI.

## Milestone 8 — iPhone bridge

- [ ] Location capability.
- [ ] Contacts capability.
- [ ] Calendar read/create.
- [ ] Photos picker.
- [ ] Camera capture.
- [ ] Notifications.
- [ ] Phone broker event flow.

## Milestone 9 — Feedback/evals

- [ ] Feedback events.
- [ ] UI thumbs/correction.
- [ ] Task outcome scoring.
- [ ] Eval JSONL export.
- [ ] Tool-call fixture tests.

## Milestone 10 — Native/custom build

- [ ] Add expo-dev-client.
- [ ] Add Swift module if needed.
- [ ] On-device LLM prototype.
- [ ] TestFlight/dev build.

## Nice-to-have later

- [ ] Voice wake mode.
- [ ] NATS JetStream.
- [ ] Qdrant.
- [ ] Multi-machine registry.
- [ ] Agent web dashboard.
- [ ] LoRA training pipeline.
- [ ] Project-specific workers for 27PM.
