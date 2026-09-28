# Project graph, public explanations and mobile navigation

## Delivered backend

The production API is running the narrowly scoped backport
`ebd11524a545aa9da77bfe03915453af7d7b60e9-9bfb606e2961`.
Independent verification at 2026-09-28T00:02:46Z confirmed six online workers,
schema 26 and exact preservation of all 37 protected data fingerprints.
The user-authorized evaluation cancellation preserved the four saved revisions;
the cancelled project was not resumed by deployment or qualification.

The late check at 00:42:55 UTC confirmed the exact release, HTTP 200 health,
six fresh worker heartbeats (maximum age 4.723 seconds), and no current-process
ASGI exception, HTTP 5xx or restart. During cutover, the predecessor logged
`sqlite3.OperationalError: database is locked` at 00:02:08 in
`websocket_notifications.close()`, called by `main.lifespan()` while stopping.
The lock holder is not established. This transient incident is retained and
the rollout is not described as interruption-free. No mutation or inference
was performed by the late checks.

A single post-deployment, provider-only benign planner probe returned the expected
three-step plan in 26.462 seconds and 701 completion tokens, with a normal stop.
It made no project, task or tool changes. A separate read-only graph projection
against the installed service returned 10 nodes, 7 edges and the existing revision.
Production HTTP was checked for unauthenticated rejection only; authenticated HTTP
success is covered by isolated tests, not claimed as a live device result.

The isolated candidate passed 296 selected checks and 110 deployment-helper tests.
Two additional runtime assertions about expected model-call counts failed with
expected 3 versus actual 4 and reproduced unchanged on the predecessor. These
preexisting failures are retained in the planner qualification evidence; the
complete backend suite is not claimed to pass.

## Mobile source and verification

Build source: `a5bf3b5f6b4e312f21bda2b47247ad76ef386f93`.
All implementation commits are on GitHub main. The Vibecode push timed out; it is
not reported as synchronized.

The earlier UI source at 9d419f9 passed 958 tests across 58 suites, TypeScript and
ESLint. The later dependency-routing change passed 14 component tests, TypeScript,
targeted ESLint and geometric collision checks at six widths. After two wording
corrections and fixture isolation, both affected suites passed all 72 tests.
The fixture fix removes queued one-shot mock responses after a preceding failed
assertion; no controller behavior was changed for that issue.

Component-level browser QA used fictional data and a mutation-blocked local harness.
At 320 px, dependency arrows visibly bypass independent steps. Step selection,
detail dismissal and activity filtering were exercised; wide chat history retained
conversation drafts. Grouped settings retained edits. This is not physical iPhone,
keyboard, VoiceOver or TestFlight runtime qualification.

## Meaning and limitations

The graph displays persisted steps and dependencies. Public explanations from
accepted plans/replans and evaluator summaries are distinct from execution evidence.
Missing historical explanations remain missing. Internal reasoning and unrecorded
worker operations are not reconstructed. The activity feed describes the last
received state and recorded operations; it does not promise token streaming.

Criterion-to-evidence mapping is not yet recorded and check freshness for all
current files is not established. The graph improves structural visibility but
does not certify that all user requirements are met or that drift is impossible.
The current color palette is retained pending the user's comparison choice.

The next traceability increment should preserve a permanent read-only link to
the initial request and paginated source instructions, then record explicit
planner-declared source-message and criterion references per step. References
must belong to the presented conversation revision and be rejected atomically
when stale or foreign. A later user instruction should be labelled as newer
than the plan, without inferring that the agent ignored it. Existing context
snapshots preserve source messages but do not identify independently validated
atomic requirements. Historical plans must retain their missing links, and a
passing test must not automatically satisfy a requirement. This increment is
not part of the present release.

## Separate website adapter

Commit 0fa368e adds a bounded HTML source-dossier adapter with 32 passing tests.
It is not activated as an API or worker and was not included in the backend backport.
Client-site capture, public TLS qualification, versioned persistence and a website
creation interface remain separate work.

## TestFlight

Build **0.1.0 (20260928001500)** archived successfully in 1705.255 seconds.
All 206 pinned mobile source files were unchanged. The source map contains
8,276,249 bytes with SHA256
`15ca737263d2665e05f8879574cd87357383ad1b94250007c3b6a84c9764e682`.

The signed export succeeded in 14.978 seconds. The IPA contains 45,669,134 bytes,
SHA256 `e59bf10313ad8648ed5bcb4039d8741a837eb79426eae56a64f54e57b0237a43`.
The independent audit passed at 00:53:19 UTC: exact sources for fifteen UI files,
matching illustration bytes, thirteen routes, strict archive/export signatures,
release-only native listener behavior and dependency checks. The archived plist
has the expected bundle/version/build and exempt-encryption flag. Only
CFBundleVersion changed in the generated native plist.

Main-app and llama symbols match. Matching dSYMs for the prebuilt React,
ReactNativeDependencies and Hermes frameworks remain unavailable, as in the prior
release; symbolication of those images is therefore incomplete. Temporary signing
credentials were cleaned up after export. Prior source/archive/model data and the
new archive, dSYMs, raw JavaScript and source map were preserved when only explicitly
scoped compilation caches were removed.

The single upload exited 0 after 89.403 seconds, with `UPLOAD SUCCEEDED` and
`No errors uploading` markers. The uploaded IPA hash matches the audited export.
App Store Connect at 2026-09-28T00:59:07.924Z confirmed **VALID**, not expired,
**IN_BETA_TESTING** and membership in the internal **27pm** group. French Canadian
notes were applied once and read back with SHA256
`70b552fe5cbde3f3a186904d960713255a7064881dfdead05d5abac79f53983f`.
The build is available for internal TestFlight testing. External beta review was
not submitted; its external state is `READY_FOR_BETA_SUBMISSION`. No physical
iPhone execution of this build has been performed.

Private receipts are retained in
`~/Library/Developer/Xcode/SwarmerTestFlight/20260928001500/` and
`~/Library/Logs/SwarmerDeploy/project-graph-20260928/`.

### Checks for the physical iPhone

1. Open Chat, Projets, Activité and Réglages; verify safe areas, keyboard, scrolling
   and back navigation.
2. In a project, select a graph step, close its detail sheet and filter its activity.
   Confirm that unrelated steps are not connected by a crossing arrow.
3. Read the recorded explanation and its source. An older project with no recorded
   explanation must say so; it must not invent a historical reason.
4. Switch chat histories with an unsent draft, then return and verify preservation.
5. Disconnect/reconnect the configured server and verify stale labels and disabled
   actions until current authoritative data arrives.
