# Shared GPU admission — 3 October 2026

## Scope

Application changes are in commits `251b9b2` and `f78f326`. They coordinate the
Swarmer API, Chroma Studio and the image worker through one host lock and the
existing database leases. CPU speech, repository reads and research do not take
the generation slot. Model files, credentials and permission policy are unchanged.

The API setting `MONGARS_LOCAL_MODEL_GPU_LOCK_PATH` and image-worker setting
`MONGARS_MEDIA_EXTERNAL_ADMISSION=1` are paired deployment options. Deploy the
API, Studio and image-worker candidates together; retain compatible predecessors
for grouped rollback. No database restore belongs in that rollback.

The mobile media component now distinguishes blocked, skipped and waiting states.
Those mobile changes still need a new device build for runtime verification.

## Validation

- Initial complete backend suite: 3,584 passed, 11 skipped, 7 failed. The failures
  concerned media catalogue coverage, the context-evaluation fixture, published
  OpenAPI operations, planner schema size and a mixed-plan fixture.
- All seven failures were addressed. Their combined suites then passed 145 tests.
  The complete suite was not rerun after those corrections.
- Studio and media-worker suites: 185 passed. The planner graph, provider,
  evaluator and media schema suites also passed 318 tests.
- Targeted direct-model and external-admission tests cover chat, planning,
  memory normalization and repeated cancellation during SQLite commit.
- The final immutable API candidate and Studio candidate passed seven
  cross-process checks on Ubuntu, using a temporary database and a fake renderer.
  They cover an uncommitted reservation, a committed lease, a live Studio owner,
  process termination, cleanup of an owned marker, preservation of an unknown
  marker and exclusion of CPU-only skills from GPU admission.
- A separate repeated-cancellation check confirmed that the host lock remains
  held until a pending SQLite commit finishes.

These isolation checks launched no GPU inference, image, audio or production
qualification job. They are not a new end-to-end image-generation test.

## Immutable candidates

| Component | Candidate |
| --- | --- |
| API | `local-20261003-gpu-admission-2db7f5986724` |
| Studio | `20261003-gpu-admission-7ae848913838` |
| Image worker | `media-chroma-d6ff665813f9913b56a8` |

The API source archive SHA-256 is
`2db7f5986724f5dfec2d197c1c5752c4f4c0a71061d57fa1733fceda3ee670aa`.
The wheel SHA-256 is
`4af91105c1a473af725cb273356c64de88cf197993a35c474af62fc90b3484b3`.
Staging verified 120 application files and preservation of all 63 tables on a
database copy.

## Activation

The three candidates were activated together under a supervised user service.
The operation completed successfully in 7.043 seconds, without rollback or
database restoration. The operator held the existing GPU lock through startup;
Studio reported the slot reserved during this barrier and ready after release.

The post-startup receipt confirms all nine services active, seven fresh worker
heartbeats, all 52 protected fingerprints identical and all 363 existing context
rows preserved without any append. The seven retained Studio jobs and their
artifact inventory match the pre-cutover baseline.

The reviewed operator driver passed 39 failure-path tests; its structural
admission helper passed another 22. The driver SHA-256 is
`791f80e454a2b5bce7514a5070017474040d748f58cc865261f01875d34a5aa6`.
Private baseline, backup and journal are retained under
`~/.local/share/swarmer-gpu-admission-20261003/cutover-final` on Ubuntu.

An independent read-only verifier passed at 10:36:37 UTC. It checked the actual
candidate source files, both opt-in settings and the existing lock through all
three process mount namespaces (device 66308, inode 7352436). The lock was empty
and available. It independently confirmed all 52 fingerprints against the real
backup, all 363 contexts, seven Studio jobs and 37 retained Studio files. No
device-presence exception was needed; no active work or resident Ollama model was
observed. Its own 17 isolated tests and Ruff passed.

The private `independent-live-verification.json` receipt SHA-256 is
`92d34dfbbee8d8b255cf69736ab44ed097392e0eeab5b085eec6fd3f81a02204`.
The verifier SHA-256 is
`ed3e174164edd9cb936e112249f5df3f31eff4862b141f47d329cf74569921fc`.

## Cleanup boundary

The preceding user-authorized cleanup removed 42 qualification goals, their 168
internal tasks, and three qualification jobs in Chroma Studio. It preserved 83
other goals and seven Studio jobs. The GPU qualification above used only temporary
fixtures and did not repopulate that production history.
