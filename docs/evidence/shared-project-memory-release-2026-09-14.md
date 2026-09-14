# Shared project memory release — 14 September 2026 UTC

The shared project-memory change is deployed on Ubuntu and delivered to internal
TestFlight as **0.1.0 (20260914002037)**. Apple reports `VALID` and
`IN_BETA_TESTING`. Physical validation of this new build remains incomplete.

## Source and behavior

The deployed and archived source is
`0826e7303775164d724419e9d66913b5ca2983d3`, with mobile tree
`33ce6f616e5bbf7311ac917fe1b9146917aa8210`. GitHub and Vibecode `main` were
independently read back at that source after non-force pushes. This subsequent
evidence-only commit does not change the released code.

Ubuntu supplies the planner, evaluator and project workers with excerpts from
the same project-scoped semantic memory. The iPhone local planning flow retrieves
bounded excerpts and binds its reviewed plan to the current context receipt.
EmbeddingGemma 300M and the stored vectors remain on Ubuntu. Retrieval ranks
SQLite vectors by cosine similarity; it does not use the separate FAISS
projection. General memory and completed-episode embeddings remain disabled.

For a terminal goal linked to a project, **Planifier la suite sur l’iPhone**
creates a continuation with the same conversation and history. It waits for a
local plan without starting agents. **Reprendre le plan sur l’iPhone** restores
that flow after leaving the screen. Stale context and automatic start requests
are rejected before mutating the pending local plan.

Implementation and test details are in
[the shared-memory qualification](shared-project-memory-2026-09-13.md).

## Ubuntu deployment

Cutover completed at 00:36:58 UTC. Independent verification after service startup
included a fresh worker heartbeat at 00:37:22 UTC. API 0.14.2 runs the exact
candidate virtual environment and schema 24. Local and HTTPS health,
authenticated worker access, unauthenticated rejection, worker identity and the
live OpenAPI contracts passed.

The 23→24 migration preserved all 56 existing tables and pairing records before
restart. Post-start comparison found no protected data differences. Before and
after backups are retained. Recovery must use schema-24-compatible code; old
schema-23 code cannot open the migrated database. No snapshot may overwrite
writes accepted after its capture.

Deployment did not initiate inference or a CRM job. The earlier
[semantic-memory audit](semantic-memory-2026-09-13.md) records semantic payloads
in 13 claimed project-worker jobs: 12 completed and one cancelled. Reconstruction
from saved payloads and the active worker source retains those hints in all 13
prompts. Outgoing model requests were not retained, and causal influence on the
answers was not measured. That historical activity occurred on September 13
between 00:19 and 01:17 UTC.

## Qualification with the real embedding model

A fresh, coherent private copy of live schema 24 was exercised against source
`0826e730`. The canonical conversation service created a real `iphone_local`
continuation from a terminal project goal. The child retained the project,
conversation and historical messages; it had zero nodes, a null start time and
phase `awaiting_local_plan`.

The existing EmbeddingGemma endpoint returned two 768-dimensional vectors in
2.354 seconds during one batch call. The child received four project-scoped
semantic items, a current receipt and `local_planning_eligible=true`. One model
credit was charged. The next read reused the cache without an HTTP call, credit
or database change. An empty start request was refused without mutation. No
generation provider was called and no job was created.

Only the private copy was mutated. Before and after SQLite snapshots were
retained through online backup, including committed WAL state, and reopened
locally for integrity and foreign-key checks. Result SHA256:
`b41542d91542f528da66170757df0818e5291bbc8e1cbe36ccdae058315d6faa`.
After-snapshot SHA256:
`d90dea84117d432b58230eb75845dc8f043abc8d3c0531d649021947a892e90c`.
The private database copies are not committed.

## Automated checks

- Mobile: 527 tests in 36 suites passed; TypeScript and ESLint passed.
- Full server run: 1,106 passed, one 50ms notification timing failure, 9 skipped.
- The unmodified notification suite rerun passed all 10 tests. Relevant runtime
  functions match the prior deployed release; no timeout was relaxed. The full
  run is not reported as an all-green result.
- Ruff, formatting, strict typing of 61 modules, Bandit and OpenAPI checks passed.
- [GitHub run 34793131116](https://github.com/ales27pm/swarmer/actions/runs/34793131116)
  did not execute its required job because the account is locked for a billing
  issue. The local checks are separate from GitHub CI.

## Archive, export and TestFlight

The isolated builder uses Expo `~55.0.31`, React Native `0.83.10` and Xcode 26.3
with the iPhoneOS 26.2 SDK. Source and builder package versions agree. The frozen
manifest binds 119 source files, qualified native inputs from `d9da64b0`, builder
baseline `2df15455` and the distribution signing handoff. Manifest SHA256:
`dcf913d9c53d3a53d106b8331a06d4f3584659c5f3fa72b738d8e69ff1762ede`.

The Release archive completed in 2,037.6 seconds and official export in 8.8
seconds. The five-Mach-O dependency check passed. Contacts, Core ML, MLX and GGUF
runtime markers are present. Distribution signing and `get-task-allow=false`
were verified. The app and llama dSYM UUIDs match their binaries. The prebuilt
React, ReactNativeDependencies and Hermes frameworks do not supply matching
dSYMs.

The official IPA is **19,879,575 bytes**, SHA256
`f86878b830e8dd362b70ccb06a1992f9e11bbe814699f2643af2f10a547c5e21`.
Its Hermes bundle SHA256 is
`c6648a457caadaeff9a1a62f4cbac7271c2330e0ccf515172fe78ec5d89126e4`.
The Files sharing flags are enabled. Independent review compared all 119 frozen
sources to Git and all 76 IPA files to the verified exported app. Both new
continuation/reprise labels are present in the final Hermes bundle and absent
from intermediate build `00525`. The exact archived Hermes compiler
successfully disassembled both bundles. The intermediate development archive
and the final audit-only IPA were not uploaded.

One upload of the official IPA completed with `altool` exit 0 and
`UPLOAD SUCCEEDED`. Apple verification at 01:35:37 UTC reported:

| Field | Value |
| --- | --- |
| App | `6809592768`, `org.27pm.mongars` |
| Build | `20260914002037` |
| Build / delivery ID | `1606daf7-b043-4e2f-98f5-c48e31fc06b0` |
| Processing | `VALID`, not expired |
| Internal distribution | `IN_BETA_TESTING`, automatic notification enabled |
| External distribution | `READY_FOR_BETA_SUBMISSION`; no review submitted |

Approved fr-CA notes were published by one PATCH at 01:37:40 UTC and read back
exactly. Localization ID: `9ea99527-b209-4338-8ce9-242ae46de7f0`.
Text SHA256: `e0fa6b4041133efce5bcef103937f828c3c1b49ff1cfe12cdc015b975d0b878f`.
No other locale, external review or public App Store submission was changed.

The builder was restored after all compiler processes ended. The temporary
signing keychain and P12 were removed; other keychain search entries and the
login keychain ACL were preserved. Release source guards were then released.

## Physical iPhone boundary

After archive completion at about 01:24 UTC, the correct physical iPhone 16 Pro
became operational through a TCP IPv6 tunnel. Developer services, app inventory,
process inventory and private file transfer succeeded. A subsequent `xcdevice`
read also reported that physical device available.

The installed app was build `20260913203646` and was stopped. A coherent capture
at 01:26:29 UTC retained its database plus WAL/SHM sidecars from
`Documents/SQLite`; integrity passed, with 12 tables and zero outbox entries.
Original rows and primary-key fingerprints are retained privately for a future
post-migration comparison. TestFlight was opened on monGARS Swarm and still
displayed that older build.

At 01:32 UTC the user reported an installation. Fresh app inventory still
reported **`20260913203646`**, with a new bundle location and a running process.
This installation is not attributed to `20260914002037`. The developer tunnel
then became unavailable. A new opening attempt after Apple validated `02037`
failed with device-not-found / connection timeout; no install button for the new
build was pressed by the agent.

Installation of `02037`, physical migration to Application Support, model
persistence in Files, Dolphin generation with shared memory and end-to-end CRM
execution remain unverified. The real-provider qualification above validates
the service on a private copy, not these device behaviors. The next physical
test must confirm the installed build, complete the storage comparison, load
MLX, generate a valid local CRM plan, review and start it, then verify actual
worker outputs and a project-linked local continuation.
