# Local image and French speech workers

The control plane schedules two explicit skills: `audio.synthesize` and
`image.generate`. Model weights and inference stay on the operator's Ubuntu
host. The phone submits the job and retrieves its private result. This is not
on-device, offline iPhone generation: the phone still needs a connection to the
control plane.

## Execution and persistence

1. The planner produces a typed request with the text or image prompt.
2. The server validates the arguments, project requirements and worker policy.
3. A worker claims the job and maintains its lease while generating in a child
   process. The job cannot choose a model path, download URL or executable.
4. The worker validates the generated PNG or WAV and uploads the bytes with its
   claim token, lease, generation and SHA-256.
5. The server accepts a reference only to an artifact from that execution.
   Media bytes live in a private directory beside the database, outside project
   source snapshots and model context.
6. The project's authenticated owner retrieves the result. monGARS checks the
   bytes and uses a temporary local file for an image preview or audio playback.

`POST /agents/{agent_id}/jobs/{job_id}/media` accepts raw PNG or WAV, with a
maximum of 8 MiB. `GET /goals/{goal_id}/media` returns accepted references;
`GET /goals/{goal_id}/media/{artifact_id}` returns authenticated bytes. Neither
download route uses a public URL or puts credentials in a URL.

The private media directory must be included in backup/restore together with the
database. It has a bounded quota; storage exhaustion is an explicit error.
An artifact's presence alone is not proof the goal is completed.

## French voice baseline

The first profile is Kokoro 82M, French voice `ff_siwis`, on CPU. It receives the
explicit text without a language model rewriting it. Text is split into
preserved substrings to fit the phoneme context; phonemes are not silently cut.
An output exceeding its requested duration is rejected rather than published
partially. Inputs are bounded to 1,000 characters and 30 seconds; outputs are
24 kHz, mono, PCM16 WAV.

The immutable profile is [`configs/media/kokoro-fr-cpu-v1.json`](../../configs/media/kokoro-fr-cpu-v1.json).
Place it alongside its three model files. Every file has an expected size and
SHA-256. Install dependencies in a separate Python 3.12 environment; this must
not change the control plane's Python environment. The render subprocess is
configured offline and rejects Python network connections. The deployed worker
also runs it in a Bubblewrap network namespace with read-only inputs and private
scratch. Actual Ubuntu probes verified that network isolation; see the
[audio deployment evidence](../evidence/audio-swarmer-deployment-2026-10-03/README.md).

Kokoro's authors publish the model under Apache-2.0 and label this voice French
`fr-fr`; a Quebec accent is not established. See the
[model card](https://huggingface.co/hexgrad/Kokoro-82M) and
[voice inventory](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).
There is no additional language-model rewriting step. This does not establish
that a model is "abliterated", universally unrestricted, or that every word is
audibly preserved: those are separate behavioral and listening checks.

## Image backend and GPU scheduling

The initial adapter supports an installed SDXL base plus the SDXL-Lightning
four-step UNet, FP16, trailing Euler scheduling and guidance scale zero, as in
the [publisher's example](https://huggingface.co/ByteDance/SDXL-Lightning).
Its profile inventories every local dependency and pins source revisions.
Images are bounded to 512 or 768 pixels on each axis. This legacy profile uses
`model_profile: "sdxl-lightning-4step"`, or the omitted-profile default, with
exactly four steps.

An explicit `model_profile: "chroma1-hd-q4"` selects the Chroma contract: exactly
40 steps, 512 × 512, and a 600-second inclusive worker deadline. The operator
installs the hash-pinned [Chroma profile](../../configs/media/chroma1-hd-q4-sm75-v1.json),
including the compiled CUDA sm75 `sd-cli`. The native adapter uses the direct
GPU recipe qualified in the [standalone inference evidence](../evidence/chroma-inference-2026-10-02.md):
Euler/Flux, CFG 3, both shift endpoints `ln(3)`, diffusion weights/computation on
GPU, T5 and VAE on CPU, a 6.5 GiB managed VRAM budget and four CPU threads.
The budget does not impose a hard whole-device VRAM limit.

Only prompt and seed vary in this first Chroma adapter. The executable, model
files, sampler, paths and compute configuration come from trusted installed
code/profile, never the request context. The native process shares the leased
renderer process group, including TERM/KILL cleanup on cancellation or timeout.
Its PNG is decoded and verified before upload. A profile mismatch fails before
rendering; it cannot silently switch models.

Register the Chroma card with `model_id: "chroma1-hd-q4"` in the separate
registration field, outside the manifest. First deployment
exposes one image profile at a time: agent selection is still based on skill,
and multi-profile routing is not implemented. The planner sees the advertised
model ID, while the worker enforces the installed profile independently.

Image generation participates in shared GPU admission with the code models.
CPU speech does not hold that GPU slot. In addition to active jobs, warmed
Ollama models can retain GPU memory: the worker must not unload them blindly.
Unknown or occupied residency prevents a render. The image runtime is opt-in
until qualified on the target card. RTX 2070 supports the FP16 path selected
here; no BF16 or FlashAttention 2 requirement is introduced.

No weights are fetched during a job. Initial installation requires network
access to obtain pinned files; later inference uses those local files. The
SDXL adapter has not been GPU-qualified. Chroma's standalone inference has been
qualified on the target card. The deployed Chroma server/worker adapter has also
completed a registered image job, authenticated artifact retrieval and goal
evaluation/closure; see the
[image deployment evidence](../evidence/chroma-swarmer-deployment-2026-10-03/README.md).
Both the image and audio qualifications supplied the initial one-node plan;
autonomous initial planning remains a separate check.

## Qualification boundary

See [`docs/evidence/local-media-cpu-2026-10-02.md`](../evidence/local-media-cpu-2026-10-02.md)
for actual CPU measurements and their scope. Python unit tests, mobile unit
tests and a standalone synthesis do not prove production deployment or physical
iPhone playback. A new Debug build linking `expo-audio` was installed and its
on-device API startup verified. Physical image preview and audio playback
remain unqualified after subsequent API connectivity was lost. See the
[mobile readiness report](../evidence/media-mobile-readiness-2026-10-03.md).
