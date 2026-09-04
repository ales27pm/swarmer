# monGARS Swarm App — Combined Documentation Index

This file is the single-entry reading surface for the repository. The canonical, editable specifications are the individual files linked below; keeping them split avoids a giant duplicated document drifting out of sync.

## Core

- [README.md](README.md) — build kit overview, stack and reading order.
- [MASTER_SPEC.md](MASTER_SPEC.md) — consolidated system specification.
- [docs_manifest.json](docs_manifest.json) — generated documentation inventory.

## Architecture decisions

- [ADR-0001 — Local-first iPhone, Ubuntu Source of Truth](adrs/ADR-0001-local-first-ubuntu-source-truth.md)
- [ADR-0002 — Abliterated Where It Thinks, Strict Where It Acts](adrs/ADR-0002-abliterated-thinking-strict-actions.md)
- [ADR-0003 — Shared State and Memory Services](adrs/ADR-0003-shared-state-memory-service.md)
- [ADR-0004 — Message Board for Agent Swarm](adrs/ADR-0004-message-board-agent-swarm.md)
- [ADR-0005 — Expo App with Native Bridge Path](adrs/ADR-0005-expo-plus-native-bridge.md)
- [ADR-0006 — Feedback Dataset Pipeline](adrs/ADR-0006-feedback-dataset-pipeline.md)

## Specifications

- [00 — Reference Map](docs/00-reference-map.md)
- [01 — Product Vision](docs/01-product-vision.md)
- [02 — Requirements](docs/02-requirements.md)
- [03 — System Architecture](docs/03-system-architecture.md)
- [04 — Mobile Expo Architecture](docs/04-mobile-expo-architecture.md)
- [05 — Ubuntu Control Plane](docs/05-ubuntu-control-plane.md)
- [06 — Model Kit](docs/06-model-kit.md)
- [07 — Agent Swarm Protocol](docs/07-agent-swarm-protocol.md)
- [08 — State, Memory and Sync](docs/08-state-memory-sync.md)
- [09 — Permission Gateway](docs/09-permission-gateway.md)
- [10 — iPhone Native Capabilities](docs/10-iphone-native-capabilities.md)
- [11 — Feedback and Training Pipeline](docs/11-feedback-and-training.md)
- [12 — API Contracts](docs/12-api-contracts.md)
- [13 — Data Model](docs/13-data-model.md)
- [14 — Message Board Events](docs/14-message-board-events.md)
- [15 — Testing Strategy](docs/15-testing-strategy.md)
- [16 — Code Analysis and Quality Gates](docs/16-code-analysis-quality-gates.md)
- [17 — Security Threat Model](docs/17-security-threat-model.md)
- [18 — Development Setup](docs/18-dev-setup.md)
- [19 — Deployment and Operations](docs/19-deployment-ops.md)
- [20 — Roadmap and Backlog](docs/20-roadmap-backlog.md)
- [21 — Acceptance Criteria](docs/21-acceptance-criteria.md)
- [22 — Privacy and Data Governance](docs/22-privacy-data-governance.md)
- [23 — Implementation Plan](docs/23-implementation-plan.md)
- [24 — Open Questions](docs/24-open-questions.md)
- [25 — Codex / Builder Handoff](docs/25-codex-handoff.md)

## Machine-readable contracts

- [OpenAPI](api/openapi.yaml)
- [Agent Card schema](schemas/agent-card.schema.json)
- [Event Envelope schema](schemas/event-envelope.schema.json)
- [Feedback Event schema](schemas/feedback-event.schema.json)
- [Memory Record schema](schemas/memory-record.schema.json)
- [Permission Request schema](schemas/permission-request.schema.json)
- [Sync Operation schema](schemas/sync-operation.schema.json)
- [Tool Call schema](schemas/tool-call.schema.json)

## Runtime configuration

- [Feedback](configs/feedback.yaml)
- [Memory](configs/memory.yaml)
- [Message Board](configs/message-board.yaml)
- [Mobile Capabilities](configs/mobile-capabilities.yaml)
- [Model Manifest](configs/model-manifest.yaml)
- [Permissions](configs/permissions.yaml)
- [Quality Gates](configs/quality-gates.yaml)
- [State](configs/state.yaml)

## Agent/build prompts

- [Codex Build Prompt](prompts/codex-build-prompt.md)
- [Expo Builder Prompt](prompts/expo-builder-prompt.md)
- [Orchestrator System Prompt](prompts/orchestrator-system.md)
- [Code Worker Prompt](prompts/worker-code.md)
- [Files Worker Prompt](prompts/worker-file.md)
- [Phone Bridge Worker Prompt](prompts/worker-phone-bridge.md)
- [Research Worker Prompt](prompts/worker-research.md)
- [Feedback Classifier Prompt](prompts/feedback-classifier.md)
- [Testing / Code Analysis Prompt](prompts/testing-code-analysis-prompt.md)

## Test and review material

- [Acceptance Scenarios](tests/acceptance-scenarios.feature)
- [Backend Test Matrix](tests/backend-test-matrix.md)
- [Emulator QA Runbook](tests/emulator-qa-runbook.md)
- [Mobile Permission Test Matrix](tests/mobile-permission-test-matrix.md)
- [Test Plan](tests/test-plan.md)
- [Build Checklist](checklists/build-checklist.md)
- [Release Checklist](checklists/release-checklist.md)
- [Security Review](checklists/security-review.md)

## Diagrams

- [System Architecture](diagrams/system-architecture.mmd)
- [Deployment Topology](diagrams/deployment-topology.mmd)
- [Permission Flow](diagrams/permission-flow.mmd)
- [Swarm Sequence](diagrams/swarm-sequence.mmd)
- [Sync Flow](diagrams/sync-flow.mmd)

## Build invariant

**Models propose → Permission Gateway decides → sandboxed executors act.** Ubuntu remains authoritative for shared state and long-term memory, while the iPhone behaves local-first through its SQLite replica and synchronized outbox.
