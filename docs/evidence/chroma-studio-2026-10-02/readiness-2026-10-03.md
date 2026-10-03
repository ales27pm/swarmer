# Studio readiness correction — 2026-10-03

Release `20261003-readiness-f3c6d2109e84` replaces only runner.py, studio.py
and static/app.js in the preceding shared-lock release. 73 Python tests,
13 Node UI tests and Ruff passed. Unknown GPU state continues to block.
CPU-only Ollama residency (explicit integer zero VRAM) alone no longer blocks.
The API exposes `availability_code`; the header distinguishes GPU reservation,
missing runtime/model and failed checks. Unavailable status polls every 2s.

An idle Studio process was stopped only after freezing all its threads and
checking for active/partial job records and running Studio containers.
No accepted job was interrupted. The original environment, service unit,
model files and history remain unchanged. No model was unloaded.

The new copy of the deployment helper also fixes stop-state parsing: the code
after `State:` must equal T or t. Previously a substring check could mistake
the letter t in the field name for a stopped process. Deterministic tests cover
running, sleeping, uninterruptible, zombie, missing and malformed states.

Live API confirmed `ready=true`, `availability_code=ready`, no active job.
A separate HTTP read confirmed the served JavaScript SHA-256
`0f9c5d0e9333b7eeb257767f78b353f53dfce64fadda9920d085243fd9d3f3f3`
and `Cache-Control: no-store`. This is server/served-code proof, not an iPhone
screenshot of the new label. No user prompt or history image was inspected.

The actual GPU reservation observed before cutover was a resident 6.39GB Qwen
model after evaluation. Its normal expiry released the reservation; Studio
readiness returned without a forced unload.
