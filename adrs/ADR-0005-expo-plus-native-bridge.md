# ADR-0005 — Expo App with Native Bridge Path

## Status

Accepted.

## Context

Expo gives fast iteration, but iPhone native capabilities and local inference may need custom native code.

## Decision

Start with Expo Go where possible. Move to Expo development build when custom native modules are required.

## Consequences

Positive:

- fast initial development;
- keeps native path open;
- supports official Expo modules.

Negative:

- development build required for MLX/Core ML/custom bridges;
- permissions must be configured carefully.
