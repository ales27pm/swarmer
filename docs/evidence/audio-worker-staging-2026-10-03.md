# French voice worker staged and qualified offline

The audio-only preparation reuses the previously qualified Kokoro configuration,
French voice `ff_siwis`, and exact 91-package Python inventory. It does not replace
the Chroma service or its immutable source release. Registration, policy enablement,
activation, production API artifact proof, and phone playback remain separate steps.

## Staged kit

- Release: `media-audio-dba819494b6ad405254d`.
- Archive: `/tmp/media-audio-dba819494b6ad405254d.tar.gz` on Mac and Ubuntu.
- SHA-256: `2998d0a4fb8bb9a56aa393d9a09d0cac2fa0809b55cd147cdd4a9f3e3c1437c1`.
- Immutable source directory:
  `/home/ales27pm/.local/share/swarmer-audio-worker/releases/media-audio-dba819494b6ad405254d`.
- Separate venv: `/home/ales27pm/.local/share/swarmer-audio-worker/venv`.
  This is a copy of the qualified environment, with its exact freeze compared to
  `requirements-audio-cpu.txt`. `uv pip check` passed for all 91 packages. No new
  dependencies or model downloads were needed. This inventory is not a wheel hash lock.
- Separate read-only model copy:
  `/home/ales27pm/.local/share/swarmer-audio-worker/models/kokoro-fr-cpu-v1/profile.json`.
  All three file size and SHA-256 pins passed; source models remain unchanged.
- Unit: `swarmer-audio-worker.service`, installed but inactive at staging handoff.
  It uses CPUQuota 200%, MemoryMax 4 GiB, no swap, 128 tasks, no-new-privileges,
  and control-group termination. Its private credential environment is not yet created.

## Real sandbox checks

The staged renderer was invoked in Bubblewrap, with an offline network namespace,
read-only model/runtime inputs, and private writable scratch. The qualification
process was additionally limited with the same CPU and memory cgroup constraints.

- Torch: `2.8.0+cpu`; `torch.version.cuda` was null.
- External TCP attempt: errno 101, network unreachable.
- French text: « Bonjour. Ceci est un essai de voix française locale. La synthèse
  fonctionne sans connexion Internet. »
- Normal generation: **8.357 s**, producing **6.525 s** of PCM16 mono 24 kHz WAV,
  **313,244 bytes**.
- WAV SHA-256:
  `cde38f420c91f006e6e3412e9467e64de1e9eee3337a30fb99d9e3b9a996be70`.
- Same text with one-second bound: explicit `duration_limit` after **8.093 s**;
  no partial WAV remained.

This proves actual offline CPU synthesis and binary validation, not listening
quality, transcription equivalence, production upload, or iPhone playback.

## Operator API proof client

`/tmp/audio_deploy_qualification.py` is present on both hosts; SHA-256:
`63e1c59e3c35bc9587c5b724952842433bde0326a2661cff26b87102f579e6c4`.

It passed eight isolated checks against current API contracts and simulated
credential, registration, start and WAV-download boundaries. It only accepts
the dedicated QA device identity, keeps secrets and receipts mode 0600, refuses
automatic repetition of an ambiguous registration/start, and supports:

```sh
/home/ales27pm/.local/share/swarmer-audio-worker/venv/bin/python \
  /tmp/audio_deploy_qualification.py register
# After policy enablement and operator approval of activation:
systemctl --user enable --now swarmer-audio-worker.service
# Separate commands: create, start, status, verify.
```

Registration uses `/agents/register`, skill `audio.synthesize`, model ID
`kokoro-fr-cpu-v1`, and a 180-second worker bound. The supplied plan has one
French speech node; the goal uses the user-approved 100 model calls, 20 steps,
10 replans, and 86,400 seconds, allowing normal completion evaluation rather
than repeating the earlier one-call image-test budget mistake.

The `verify` command decodes the owner-downloaded WAV, checks PCM16/mono/24 kHz,
duration, size, artifact SHA-256 and unauthenticated HTTP 401. None of these live
API commands were executed during staging. Safe staging receipts and the WAV
are in `audio-swarmer-deployment-2026-10-03/` beside this document.

## Additive permission transition prepared

`/tmp/audio_policy_transition.py` and `/tmp/permissions-audio-candidate.yaml`
are staged on Ubuntu. The script SHA-256 is
`db23f4a9c527a72d19c5ea42b3cea2f895d0280f8b43296db31fed0e25df52d5`.
The candidate SHA-256 is
`f61067745b0eb2dc5f61208d6a2eb319bf830bc3baad5bf5ea695fe46b3837d8`.

The proposed transition is epoch 8 to 9, adding only `audio.synthesize` and
preserving all 21 existing rules, including `image.generate`. The new canonical
rules digest is `sha256:c9c650a369c18ec44e5da67d79c3497475c938150b6ead9b4f0d65c755c3bb56`.
Eleven targeted tests passed for the actual policy differential, exact reviewed
history, rejecting a new descendant/active call/active job, and retaining any
unrelated pending goal in admission.

The live read-only probe passed before handoff. Its bounded admission view
reconciles only three historically pending ancestors of the earlier image QA
chain whose final descendant is `budget_exhausted / auto_continuation_stopped`.
Every goal/link/conversation/node/job/model-call record in that exact chain is
fingerprinted; no DB state is rewritten and the existing admission files remain
unchanged. All other work checks use the original guard and remain blocking.
The policy operator makes a full SQLite backup and rechecks live state immediately
before replacing the YAML, then verifies the protected table inventory, complete
retention of prior context rows, stable device identities, and exact policy epoch.

At this handoff, only `probe` was executed. Policy application, registration and
service activation are still coordinated separately by the parent operator.

## Live policy applied and independently verified

The parent operator applied epoch 9. The original post-check rejected passive
context appends because its old hardcoded context signature was stale. That
failed check was retained. A separate verifier compared all 10,085 backup rows
against the exact live prefix, then admitted only 24 copies of the latest passive
planner context already in that same backup (only ID and timestamp differ).
The passive reference itself is pinned by SHA-256; this is not a general allowance
for arbitrary context changes. Six targeted tests reject modified history,
changed appends, another goal, a stale timestamp and a changed reference pin.

The independent live verification passed: all 52 protected fingerprints checked;
only policy, device presence and those identical appended contexts differ.
Device identities, all earlier worker rules and image permission are preserved.
No job, model call, node or task was active. The verifier did not modify the DB,
old receipts, policy or admission helper. Registration/activation remain separate.

Verifier: `/tmp/audio_policy_independent_verify.py`, SHA-256
`ca4f3ace01418331cda6622f7242af59a0d3ad5ad7a1962089d0cb7952211b4a`.
Proof: `audio-swarmer-deployment-2026-10-03/policy-verified-independent-context.json`.
