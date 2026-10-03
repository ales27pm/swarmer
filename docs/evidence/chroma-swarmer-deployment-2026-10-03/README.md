# Swarmer Chroma backend deployment — 2026-10-03

User-authorized deployment of the Chroma image-generation integration. This
record separates production API/worker proof from the pending native iPhone UI.

## Deployed components

- API release: `local-20261003-chroma-media-0d23f123a64a`.
- Immutable source snapshot: `599dd315384f79458a04fa7efdc2189acb073b07`.
- Chroma worker release: `media-chroma-ee3d039ac6215a5cd2d7`.
- Chroma Studio release: `20261003-gpu-lock-bea3d32c73ec`.
- Image model: `chroma1-hd-q4`, 512 × 512, 40 steps, on the existing RTX 2070.

The API was built from its exact deployed predecessor with only the reviewed
media changes. The 119 installed application files match the source and wheel.
No schema migration or database restoration was performed. Existing worker
code, credentials, configuration and unrelated iOS changes were preserved.

Only the `image.generate` permission was added. All prior rules are identical;
the policy epoch advanced from 7 to 8 through the normal live reload. Audio
generation remains disabled. The separate policy proof compares every protected
table and all historical context rows, including the pre-transition backup.

## Deployment guard reconciliation

The initial attempt stopped before cutover because an existing passive project
periodically appends identical planner contexts without starting a model call.
Every original row was retained. A narrowly bounded reconciliation checks the
exact known goal, content/provenance digest, stable fields and append order;
other context additions or changes remain rejected.

The first implementation of that reconciliation had a verification gap when
post-restart data returned to the original baseline after losing newer backup
rows. Review identified it; both entrypoints now always compare against the
complete pre-cutover backup. Two regression cases and 36 focused tests cover
the correction. The original release kit and receipts remain immutable; a
corrected standalone verifier was run against the actual deployed release.

The independent production check at `2026-10-03T04:25:29Z` confirmed all six
existing services, schema 27, zero active jobs and the correct installed source.
51 protected fingerprints matched exactly; all 8,508 baseline and backup
contexts remained intact, plus nine identical passive-context additions.
See `api-verification.json`. This pre-registration proof intentionally predates
the authorized new worker registration and qualification goal.

## Worker isolation and resource sharing

The new worker uses a dedicated Python 3.12.13 environment and Pillow 12.3.0.
All three model files and the native executable are pinned by size and SHA-256.
The native renderer runs inside Bubblewrap with no network, read-only runtime
and model inputs, writable temporary output, and bounded resources. Real Ubuntu
probes confirmed network isolation and private-configuration exclusion.

The worker and Studio share an exclusive GPU lock. The worker acquires it before
claiming a task and retains it until cleanup/result submission; an orphan Studio
container also prevents a new claim. Studio was updated only after its active
user generation finished. The private Tailscale binding is unchanged.

## Validation scope

- Exact API candidate: 268 focused tests; Ruff passed.
- Media worker: 89 tests; Ruff passed; real Ubuntu isolation probes passed.
- Studio shared lock: 41 tests; Ruff passed.
- Deployment context reconciliation: 36 tests and independent review.
- Operator qualification client: eight tests against the current API contracts.

The native iPhone media UI has not been rebuilt or installed by this deployment.
Local tests and backend qualification do not constitute physical-device proof.

## Real image qualification

An isolated QA device created a manual, one-step goal through the normal API:
`goal_ac7b7f6b2c49412b868a81dd26f61bed`. Its sole image task is
`job_0bbbeb2d86d443aa8d647504ff90459e`. The prompt requests a red ceramic teapot;
it uses seed 42, 40 steps and 512 × 512 pixels. It is a worker/transport test,
not a claim that autonomous planning was tested.

The single job completed successfully on its first attempt at
`2026-10-03T04:31:50.946212Z`, after **243.603335 seconds** of execution and
2.127819 seconds in the queue. Its error and last-failure fields are null.

The authenticated download returned a valid, fully decoded 512 × 512 PNG of
407,853 bytes. SHA-256:
`77c3de68e3e6eedbffe93032eb0f35c8d59f28f193907963d55a0289f6910bf9`.
The response and recorded artifact hashes match. Anonymous access returned
HTTP 401 and the owner response specified `Cache-Control: private, no-store`.
Visual inspection confirmed the requested red teapot on a wooden surface;
the image is neither blank nor a placeholder.

See `qualified-image.png`, `verified-image.json`, `job-timing.json` and
`worker-monitor.jsonl`. The monitor recorded real GPU utilization and no OOM
events. Cgroup memory includes model-file cache and must not be interpreted
as process RSS.

The enclosing QA goal finished as `budget_exhausted` under the overly restrictive
one-step/one-model-call test budget, with its image node completed and result
present. Its precise reason was `goal model call budget exhausted`: the image
consumed the allowed call, leaving no evaluator budget. Automatic continuation
then created three descendant planning runs (one failed planning call and two
completed calls), but **no second image job**. The last descendant reached
`auto_continuation_stopped` after three runs without completed work. A final
read-only audit found no active job/model call in this QA chain; the monitoring
process exited normally. See `goal-outcome.json`.

The image job was not retried. This is successful image-worker, binary-storage
and authenticated-delivery qualification; autonomous goal closure is not
validated by this run. Future goal-level qualification must reserve a sufficient
budget for evaluation and account explicitly for automatic continuation.

After completion, all seven Swarmer services (the six existing services plus
Chroma) and the separate Studio service were active. `/health` returned
`status=ok`. The web Studio remains at `http://100.125.44.127:8765/` through the
private Tailscale network. The image qualification was driven by an isolated
QA API client, not the physical iPhone.

## Follow-up: complete image goal with sufficient evaluation budget

At `2026-10-03T08:38:28.934284Z`, a new goal
`goal_6763fd9cd11141fb953647b55e5e436e` completed normally: one image
node, three counted model calls, zero replans, no failure, and evaluator status
`done`. Its supplied manual initial plan ran in assisted mode with the requested
limits: 100 model calls, 20 steps, 10 replans and 86,400 seconds.
This validates execution, artifact delivery and subsequent evaluation/closure;
it does not validate autonomous creation of the initial plan or physical iPhone UI.

Elapsed time from start through closure was 347.52 seconds. The single PNG has
the same 407,853 bytes and SHA-256 as the earlier fixed-seed qualification.
Authenticated download and anonymous HTTP 401 were rechecked. No extra image
node was created. See `full-goal-qualification.json`. This successful follow-up
does not rewrite the earlier budget-exhaustion result.
