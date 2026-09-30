# Repeated project reads: cutoff removal deployment

The user requested removal of the three-repeat file-read pause shown in the
project conversation, then explicitly requested deployment.

## Scope and source

The API candidate `fba27a04fec76209e82050c91b031639512a882b` is based on the exact deployed predecessor
`c48f40352cb03eb363f5636cd1ef44f1d9f97197`. Only
`server/app/services/goal_manager.py` changes in the runtime wheel. The source
archive also includes the focused regression tests and project documentation.
Other local work in progress is excluded.

Repeated file reads can continue within the existing overall step, runtime and
model-call budgets. The separate pause for three unchanged implementation
attempts remains in place. Reads preserve source/check receipts and do not erase
those failed implementation attempts. Already-paused projects require a new
message to resume; deployment did not resume or alter a user project.

## Qualification

- Exact deployed-base candidate: **186 tests passed**, covering repeated reads,
  reordered/alternating selections, native authoring, model budget exhaustion,
  duplicate results, lease rollback, lifetime revisions and runtime recovery.
- Adapted deployment helpers: **155 tests passed**, independently rerun by the
  rollout coordinator. Admission and website-writer protection remain intact.
- Runtime and changed helper lint passed. The isolated predecessor retains an
  existing I001 import-order finding in `test_native_project_authoring.py`,
  reproduced against the deployed base; no unrelated formatting was deployed.
- Wheel/source application files match byte for byte. Comparing with the active
  predecessor wheel found only `app/services/goal_manager.py` and its wheel
  `RECORD` changed. Dependency metadata is identical.
- Ubuntu staging used a private database copy and preserved all **61 tables**,
  schema 27, permission state and settings. No dependency installation or schema
  change was required. The staged rollback reproduces the predecessor runtime.

## Activation and independent verification

The supervised rollout exited 0 and reached `complete`. At
**2026-09-29T03:22:33.888880+00:00**, independent verification confirmed:

- Active release `fba27a04fec76209e82050c91b031639512a882b-575d1e14af8d` and healthy API `0.14.2`.
- Six agents online with fresh heartbeats; worker source, bindings, credentials,
  models and settings preserved.
- All **44 protected data fingerprints** unchanged; all eight admission counts
  zero. No database restoration, agent re-enrollment or project resume.
- API process PID `2450135` uses the exact candidate
  release directory.

This is a backend-only deployment. No iOS binary or TestFlight submission was
required or performed. User-project completion and physical-device behavior
were not exercised by this deployment.

## Artifact receipts

- Source archive SHA-256: `2a13c5b82b3e00d8c71c541475572d411931bc253b90f0d079b0769b1ede6639`.
- Wheel SHA-256: `575d1e14af8da7249e0353ad733b020e2c9babef3ec4c9a66e02f25dbb220e31`.
- Runtime `goal_manager.py` SHA-256: `0ad3fd6c5a7869dfdee05afe924ce7b3bed0e9083d24c66cbf5a2314476de748`.
- Reviewed baseline SHA-256: `7942d8699f55e59717844387ab86acad19ef76d6b6542f3fe637782f1d646fc7`.
- Cutover receipt SHA-256: `b8331ee5ba492af0d7d9cf399a4f980c85ef8d86cd1192d950ffd0612f488638`.
- Independent receipt SHA-256: `7ba9692ae8f4a2820ac088945da33a97602d1351b59e9d42f19cfcd12e204678`.

Private artifacts and receipts are under
`~/Library/Logs/SwarmerDeploy/remove-read-cutoff-20260929/`.
