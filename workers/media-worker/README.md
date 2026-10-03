# Local media worker

One process advertises **one** skill and uses one administrator-installed model
profile. Run separate instances for French voice on CPU and images on CUDA.
The leased control-plane protocol is reused from the shipped sibling
`../file-worker/file_worker.py`; deploy that file with this directory.

## Current qualification

Kokoro synthesis was exercised on Ubuntu with Python 3.12.13 and the exact
`requirements-audio-cpu.txt` inventory. A standalone worker render produced a
24 kHz mono PCM16 WAV of 5.65 seconds; a one-second bound rejected the same
request as `duration_limit` and left no published partial WAV. This is runtime
evidence for the original CPU synthesis benchmark, not phone playback or a
listening evaluation. See `docs/evidence/local-media-cpu-2026-10-02.md`.
The separate audio worker has since completed a registered job, private WAV
download and goal evaluation/closure; see the
[audio deployment proof](../../docs/evidence/audio-swarmer-deployment-2026-10-03/README.md).

The SDXL-Lightning adapter is **not yet CUDA-qualified**. Its enable flag defaults
to false. Tests use real small PNG/WAV files and fake inference/control-plane
boundaries; they do not establish image generation quality, latency or VRAM fit.

The Chroma adapter reuses the recipe qualified with Chroma1-HD Q4 on the RTX 2070:
complete 512-pixel images, cancellation and recovery were measured in
[`chroma-inference-2026-10-02.md`](../../docs/evidence/chroma-inference-2026-10-02.md).
Those runs used the standalone qualification harness. The deployed worker has
since completed a registered image job, private PNG download and normal goal
evaluation/closure; see the
[image deployment proof](../../docs/evidence/chroma-swarmer-deployment-2026-10-03/README.md).
Both media qualifications supplied the initial plan. Autonomous initial planning
and physical phone preview/playback remain separate checks.

## Requests and results

Voice receives `text` (1–1,000 characters), `language: "fr-FR"`,
`voice: "ff_siwis"`, and `max_duration_seconds` (1–30). Text is passed verbatim
to phonemization, divided into preserved substrings only when phonemes exceed
the model context. Neither text nor phonemes are silently truncated. Unknown
phonemes or duration overflow reject the result. Preservation of source text
does not by itself prove every word sounds correct; listening is separate.

Image receives `prompt` (1–2,000 characters), `width` and `height` (512 or 768),
`steps`, and `seed` (0–2,147,483,647). Optional `model_profile` is an exact public
profile ID, not a model path. Omitted IDs retain `sdxl-lightning-4step` for legacy
requests. The SDXL profile requires **exactly four steps** and rejects any other
count. It uses the SDXL-Lightning four-step UNet, FP16,
trailing Euler scheduling and guidance scale zero. It does not require BF16,
FlashAttention 2 or remote model code.

`chroma1-hd-q4` requires **40 steps and 512 × 512**. A request for another installed
profile is rejected as `model_unavailable`, before native execution. Context
cannot select another profile or change these bounds.

The server may attach canonical project `context`; it is retained by the server
but cannot override these explicit arguments, model paths or runtime options.
There is no additional language-model rewrite in either worker.

Outputs are PNG or PCM16/mono/24 kHz WAV, at most 8 MiB. The worker uploads raw
bytes to `/agents/{agent_id}/jobs/{job_id}/media` with bearer authentication,
claim/lease/generation headers and SHA-256. It validates the server's exact
11-field reference against the bytes, job and goal, then returns only:

```json
{"schema_version":"1.0","content_trust":"untrusted","artifact":{"...":"server-verified reference"}}
```

No binary/base64, public URL, prompt or model output is placed in worker logs.
Failure exports a fixed code. Losing or cancelling the lease discards the result
and terminates only this worker's child process group. Uploaded orphan cleanup
is a server concern. A network failure with unknown outcome is left for lease
recovery; the worker does not invent a completed result.

## Install the CPU environment separately

Use an administrator-created Python **3.12.13** virtual environment. Do not
install into the control plane environment. The checked-in inventory is the
complete public version freeze of the qualified Ubuntu environment. Install the
CPU Torch wheel from its single official index first, then the remaining exact
pins from PyPI. There is no mixed-index best-match policy:

```sh
python3.12 -m venv /absolute/private/media-audio-venv
/absolute/private/media-audio-venv/bin/python -m pip install --no-deps --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0+cpu'
/absolute/private/media-audio-venv/bin/python -m pip install --no-deps -r workers/media-worker/requirements-audio-cpu.txt
/absolute/private/media-audio-venv/bin/python -m pip check
```

This is a version freeze, **not a wheel hash lock**. Archive the resolved wheels
with hashes in the deployment receipt before installation on another host.
`espeakng-loader` supplies the local library and language data; the child configures
`EspeakWrapper` explicitly, with no global apt install.

Copy the three approved Kokoro files and
`configs/media/kokoro-fr-cpu-v1.json` into an administrator-owned model directory,
with the profile next to `config.json`, `kokoro-v1_0.pth` and `voices/ff_siwis.pt`.
The profile pins repository revision, file sizes and SHA-256. Keep the directory
read-only to the worker account. The worker rechecks every file before inference.
No request can supply a download URL, headers, command, model path or revision.

## Prepare the image profile before enabling it

### Chroma1-HD Q4 on the qualified RTX 2070

Use [`configs/media/chroma1-hd-q4-sm75-v1.json`](../../configs/media/chroma1-hd-q4-sm75-v1.json).
Its three weights and the compiled `sd-cli` are pinned by exact size and SHA-256;
all four publisher/source revisions are recorded. This is specifically the
qualified Linux/CUDA sm75 executable, not a cross-platform runtime package.
Create an immutable operator-owned directory with this layout:

```text
profile.json
models/Chroma1-HD-GGUF/Chroma1-HD-Q4_K_M.gguf
models/t5-v1_1-xxl-encoder-gguf/t5-v1_1-xxl-encoder-Q5_K_M.gguf
models/Chroma/ae.safetensors
runtime/sd-cli
```

Copy existing verified files to those paths; symlink aliases are rejected.
Keep `runtime/sd-cli` executable and all files read-only to the worker account.
The runtime's native loader dependencies must also be provisioned by the
operator, as established by the [runtime receipt](../../docs/evidence/chroma-runtime-2026-10-02.md).
There is no downloading, building, dynamic shell or package installation in a job.
The Python adapter needs only Pillow from `requirements-chroma.txt`; do not
install the SDXL Torch/Diffusers environment for this backend.

The fixed recipe uses Euler/Flux, CFG 3, diffusion flash attention and both
scheduler shift endpoints set to `ln(3)`. It computes and keeps diffusion
weights on CUDA0; T5 and VAE compute and keep weights on CPU. Its 6.5 GiB managed
CUDA budget is not a hard whole-device VRAM cap. Prompt and seed are separate
literal argv values; a job cannot append flags or choose paths. Native inference
stays in the existing renderer process group, so lease loss, wall timeout or RSS
overflow terminates the Python wrapper and native descendants, including a
descendant that ignores SIGTERM. Only validated PNG bytes may be uploaded.

Register `agent-card-chroma.json` with the separate `POST /agents` registration
field `model_id: "chroma1-hd-q4"` (not in the static manifest) and use
`.env.chroma.example`; its inclusive deadline is 600 seconds, including hash
checks and loading. The planner selects only the advertised profile. The first
deployment should expose **one image profile at a time**: dispatch is skill-based,
not a model-profile router. Registering Chroma and SDXL simultaneously can route
a request to the wrong worker, which safely rejects it rather than switching
models silently. Multi-profile routing remains separate work.

### SDXL-Lightning (retained)

Create another isolated Python 3.12 environment. `requirements-image.in` pins the
adapter's direct dependencies, but is **not a qualified transitive freeze**.
Resolve and archive that environment, verify the driver/CUDA wheel supports the
target RTX 2070 (sm75), run `pip check`, and record a real offline image before
setting `MONGARS_MEDIA_GPU_ENABLED=1`. Use a single official PyTorch CUDA index
for Torch, not a competing extra index. No package installation occurs in a job.

An administrator obtains a fixed 40-character revision of both
`stabilityai/stable-diffusion-xl-base-1.0` and `ByteDance/SDXL-Lightning`, verifies
the publisher's file identities, and assembles a new immutable directory:

```text
profile.json
base/model_index.json
base/scheduler/scheduler_config.json
base/tokenizer/{tokenizer_config.json,special_tokens_map.json,vocab.json,merges.txt}
base/tokenizer_2/{tokenizer_config.json,special_tokens_map.json,vocab.json,merges.txt}
base/text_encoder/config.json
base/text_encoder/model.fp16.safetensors
base/text_encoder_2/config.json
base/text_encoder_2/model.fp16.safetensors
base/vae/config.json
base/vae/diffusion_pytorch_model.fp16.safetensors
base/unet/config.json
sdxl_lightning_4step_unet.safetensors
```

This is a component checklist; use the exact inventory required by the selected
revision. Do not copy Python files, pickle checkpoints or unrelated weights into
`base`. The Lightning UNet replaces the base UNet, so its base weights are not
needed. The JSON profile has the same schema as the CPU profile, with:

```json
{
  "schema_version": "1.0",
  "backend": "sdxl-lightning",
  "origins": [
    {"repo_id":"stabilityai/stable-diffusion-xl-base-1.0","revision":"<verified full SHA40>"},
    {"repo_id":"ByteDance/SDXL-Lightning","revision":"<verified full SHA40>"}
  ],
  "components": {"base":"base","unet":"sdxl_lightning_4step_unet.safetensors"},
  "steps": 4,
  "files": [{"path":"<each relative model file>","size_bytes":1,"sha256":"<verified SHA256>"}]
}
```

Placeholders are intentionally invalid: replace them from the verified download
receipt. List **every file** below `base` plus the four-step UNet. The worker
rejects symlinks, traversal, scripts, unlisted files, size/hash differences and
missing components. The manifest is operator trust, not proof of publisher trust
by itself. The runtime loads only local safetensors and fixed installed code.

## Scheduling, bounds and operation

The server coordinates image jobs with other active GPU work. Before claiming an
image job, the worker queries local Ollama `/api/ps`; unknown or positive GPU
residency keeps its configured capability online but consumes no claim or attempt.
An explicitly disabled image profile stays busy/unavailable. It checks again after claim.
A race at that point fails explicitly as `resource_busy`. The worker never
unloads external models, so a warmed Ollama model can leave image work waiting
until an administrator's coordinated scheduler releases it. There is no hidden
automatic GPU eviction or claim of successful retry.

Audio is CPU-only and does not take the GPU slot. Inference runs sequentially in
a fresh process with two CPU threads (four for native Chroma), an inclusive wall deadline (1–600 seconds),
a Linux process-group RSS watchdog (4 GiB audio, 12 GiB image by default) and an 8 MiB file limit.
The image loader creates meta parameters and assigns the FP16 checkpoint directly,
avoiding an initial full FP32 copy. The RSS watchdog does
not count VRAM, kernel allocations or native services outside the child group;
set operating-system cgroup memory/process limits for the deployed service.
Model files must remain immutable during a job. Private scratch is deleted after
each attempt.

Children inherit no cloud credentials and set Hugging Face/Transformers offline
flags. A Python audit hook rejects network connects/resolution. Deployed Linux
workers set `MONGARS_MEDIA_SANDBOX=1`: Bubblewrap places the renderer and native
descendants in an offline network namespace with read-only code, interpreter,
libraries and model files, GPU device access, and only the attempt scratch writable.
Missing or failing Bubblewrap never falls back to an unsandboxed render. The
trusted parent retains its fixed control-plane and loopback Ollama connections.
No arbitrary shell command is accepted.

Set `MONGARS_MEDIA_GPU_LOCK` to the same operator-owned lock file used by Chroma
Studio (`CHROMA_STUDIO_GPU_LOCK`) and the API (`MONGARS_LOCAL_MODEL_GPU_LOCK_PATH`).
All three services must see the same local inode, owned by their shared operator
account; use a private directory and mode 0600 for the file. Never unlink, replace
or truncate it while any of these services is running. Any nonempty content is
an unresolved Studio ownership marker: the worker fails closed even if the flock
is free after a process crash. Only Studio's verified cleanup may clear its marker.
A separate read-only Docker inventory check also blocks orphan Studio containers;
an unavailable Docker inventory fails closed. Neither check unloads models.

`MONGARS_MEDIA_EXTERNAL_ADMISSION=1` enables the coordinated protocol:

1. The image worker probes the flock and releases it before asking for a job.
2. The API holds a short nonblocking flock through the SQLite claim transaction's
   commit or rollback. A busy slot defers admission before consuming an attempt.
3. After claim, the worker starts lease renewal, then acquires the renderer flock.
   This handoff waits at most 10 seconds and checks the lease throughout; timeout
   reports `resource_busy` for that one claim, without a second claim or hidden retry.
4. The worker holds the flock through renderer cleanup and result submission.
   Studio rechecks database admission after acquiring the same flock, so it sees
   the committed image lease before starting its own renderer.

This flag requires a **coordinated rollout** of the compatible API, Studio and
image worker. Stage all artifacts first; at idle, stop the image worker, configure
the API lock path and worker opt-in together, activate the compatible services,
then start the worker. Do not enable the API lock while a legacy preclaim-locking
worker continues polling: it would block its own claim. Rollback must likewise
restore a compatible configuration set, with the worker stopped during the switch,
preserving the lock and any unresolved marker. Existing lease and cleanup checks
still apply; a terminal HTTP response does not prove Ollama released GPU memory.

With the opt-in omitted or `0`, the worker retains its legacy behavior: acquire
before claim and hold until the attempt finishes. Audio synthesis ignores this
setting, never takes the GPU flock, and remains on CPU. The shared-lock protocol
does not change its model, dependencies or limits.

Register the corresponding agent card, configure the environment using
`.env.example`, and start with the chosen environment's Python:

```sh
/absolute/private/media-audio-venv/bin/python workers/media-worker/media_worker.py --once
```

Omit `--once` for polling. Set separate worker IDs, profiles and credentials for
the two instances. The image card's 600-second bound must match the operator's
`MONGARS_MEDIA_TIMEOUT_SECONDS`; audio defaults to 180 seconds. There is one job
per process. Cards and environment files do not deploy or register themselves.

## Tests and primary references

```sh
server/.venv/bin/python -m pytest -q workers/media-worker
server/.venv/bin/ruff check workers/media-worker
```

References: [Kokoro model](https://huggingface.co/hexgrad/Kokoro-82M),
[Kokoro implementation](https://github.com/hexgrad/kokoro),
[SDXL-Lightning four-step recipe](https://huggingface.co/ByteDance/SDXL-Lightning),
[Diffusers local loading](https://huggingface.co/docs/diffusers/using-diffusers/loading),
[PyTorch wheel variants](https://pytorch.org/get-started/previous-versions/).
Model licenses and behavioral labels must be checked per pinned release; this
adapter does not establish universal "uncensored" or "abliterated" behavior.
