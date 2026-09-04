# swarmer

monGARS Swarm App — architecture and build kit for an iPhone control surface connected to an Ubuntu-hosted distributed agent swarm.

## Current build kit

The complete documentation package is committed at:

- `mongars-swarm-app-docs.zip`

It contains the architecture specs, Expo/iPhone design, Ubuntu control plane, shared state, semantic memory, message board, permission gateway, model manifest, feedback/dataset pipeline, API contracts, schemas, prompts, diagrams, tests, ADRs, security notes, and implementation roadmap.

## Core architecture

- iPhone: Expo/React Native UI, local cache/state replica, native capability bridge, local inference where appropriate.
- Ubuntu: source of truth, orchestrator/control plane, shared state, long-term memory, message board, feedback service, audit log.
- Swarm: remotely reachable specialized workers registered with the control plane.
- Memory: short-term context + structured shared state + vector embeddings + artifact storage + append-only events.
- Permissions: models propose actions; a deterministic gateway authorizes, requests approval, or blocks execution.
- Feedback: task outcomes and explicit corrections feed eval datasets and reviewed fine-tuning/LoRA candidates.

## Guiding principle

> Abliterated where it thinks; strict where it acts.

No live self-training without review/versioning/rollback. No hidden autonomy. Native iPhone access remains subject to iOS permissions and platform constraints.
