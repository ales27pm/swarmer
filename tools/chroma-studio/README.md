# Chroma Studio

Small private web interface for the qualified Chroma1-HD model on Ubuntu. It serves a responsive French prompt form, measured generation phases, cancellation, persisted image history and PNG download. The model runs locally; the browser never receives server paths or executes commands.

## Runtime

Python 3.12, the pinned `requirements.txt`, Docker GPU access, and the previously qualified Chroma runtime/model files are required. Run one Uvicorn worker only:

```sh
python -m uvicorn studio:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log
```

`chroma-studio.service` expects the application in `~/.local/share/chroma-studio/current`, a dedicated environment in `~/.local/share/chroma-studio/venv`, and configuration in `~/.config/chroma-studio/studio.env`. The latter defines exact `CHROMA_STUDIO_ALLOWED_HOSTS` and `CHROMA_STUDIO_ORIGINS`. For the preferred HTTPS deployment, publish through a separate **private Tailscale Serve HTTPS port** and keep Uvicorn bound to loopback. Do not publish with Funnel.

The unit above is the preferred HTTPS deployment template. The initial Ubuntu
installation instead uses a user-service override bound exclusively to its
Tailscale IPv4 address because changing Serve requires administrator access.
Its URL is `http://100.125.44.127:8765/`: HTTP inside the encrypted Tailscale
network, not HTTPS. Exact Host/Origin allowlists match this address. It never
binds to `0.0.0.0` or the LAN address, and the existing Serve/Funnel configuration
is unchanged. Peers allowed by the tailnet ACL can access the studio and history;
there is no additional per-user login. See the deployment qualification receipt
for the actual binding and release, rather than assuming the template is active.

Generation uses fixed 512 × 512 dimensions, one image per job, 1–40 steps (40 by default), a user seed or a generated seed, and an optional negative prompt. The qualified Euler/CFG/Flux/attention and GPU-placement settings are not browser-controlled. Each Docker renderer is isolated, has no network, and has a 15-minute deadline and a 12 GiB memory limit. A single active studio job is allowed. Studio, the compatible Swarmer API and the image worker coordinate GPU admission through the shared protocol below. Studio also checks production work and Ollama residency before and during rendering, yielding by cancelling only its own container if another workload is detected. Processes outside this protocol are observed through these checks; they are not automatically coordinated.

The state directory defaults to `~/.local/state/chroma-studio`. Completed images and requests persist after a service restart; interrupted jobs become explicitly failed. Cleanup failures prevent a new render until the abandoned studio container has been removed. CSRF tokens, exact origin and Host checks protect mutations; Tailscale membership controls network access. This is a private, single-user tool, not a public multi-tenant product.

## API

- `GET /api/status`: readiness, active job, recent jobs, bounds and a CSRF token.
- `POST /api/jobs`: create a generation from `prompt`, `negative_prompt`, `steps`, `seed`, `width` and `height`.
- `GET /api/jobs/{id}`: durable job status.
- `POST /api/jobs/{id}/cancel`: stop that job; terminal jobs remain unchanged.
- `GET /api/jobs/{id}/image?download=1`: completed image attachment.

POST requests require same-origin JSON and `X-CSRF-Token` from the current status response. The browser sends argv values, not shell code. Native model stderr and server paths stay in private diagnostic files.

## Verification

Backend tests use a fake renderer and small native fixtures to verify contracts, cancellation, restart and singleton behavior, private access checks, malformed PNGs and cleanup failures. They do not replace a real generation through the browser. The separately recorded deployment qualification covers the live interface and renderer path.

The initial teapot preview is labelled **Exemple local** and comes from an actual Chroma qualification. It is not a generated job or a fabricated history entry. History only shows images created through this service.

## Swarmer integration

The studio is an independent service and does not modify the production database. The parallel Swarmer integration adds an explicit `chroma1-hd-q4` media profile and artifact support through existing goal APIs. Studio jobs are not currently Swarmer goal jobs; their history is separate.

All participating services use one operator-owned regular file, normally
`~/.local/state/swarmer-gpu/image-generation.lock`, in a private directory with
mode `0600`. Configure the same **absolute path and local inode** with
`CHROMA_STUDIO_GPU_LOCK`, `MONGARS_MEDIA_GPU_LOCK` and
`MONGARS_LOCAL_MODEL_GPU_LOCK_PATH` (API). Never unlink or replace that file.
Enable `MONGARS_MEDIA_EXTERNAL_ADMISSION=1` for the compatible image worker
alongside the API path. An occupied lock rejects Studio creation with HTTP 409;
an inaccessible lock returns HTTP 503.

The coordinated admission sequence is:

1. The API acquires a nonblocking exclusive `flock` while reserving a local model
   call or claiming a GPU worker job. It holds the lock until the SQLite transaction
   commits or rolls back. Contention defers admission without spending an attempt.
2. The image worker probes and releases the lock before requesting a claim. After
   claim it renews the lease, then acquires the renderer lock with a bounded
   10-second handoff. Failure reports `resource_busy` for that claim, without a
   hidden second claim. It holds the lock through cleanup and result delivery.
3. Studio acquires the same lock and **rechecks database/model readiness under it**
   before accepting a job. It therefore observes a backend reservation committed
   between its first readiness check and lock acquisition.

Before starting its rendering thread, Studio writes and `fsync`s this nonempty
reservation into the locked file:

```text
chroma-studio-v1:<first 16 hex characters of SHA256(resolved state-directory path)>\n
```

Any nonempty contents block API/worker admission, even after a Studio process
crash releases `flock` while a detached Docker renderer survives. Studio clears
and `fsync`s only its **exact instance marker**, under the same lock and after
its scoped container cleanup is confirmed; the inode is retained. Cleanup
uncertainty keeps the marker and blocks new work. Startup recovery never removes
another instance's or an unknown marker. If another process holds the lock at
startup, Studio remains accessible and defers cleanup until a later readiness
check or create request can acquire it. Successful recovery restores readiness
without submitting a generation; failed cleanup remains unavailable and retryable.
The worker's independent orphan-container inventory remains an additional guard.

Direct local chat, direct task planning and memory translation/presentation also
use API admission. They retain the host lock through the model request because
they have no goal-model lease. GPU contention returns HTTP 503 for direct chat
and planning (`Retry-After: 5`, `X-Mongars-Resource: local_gpu_busy`); direct chat
checks admission before appending the user's message or creating its conversation.
It therefore adds no chat messages when rejected as busy. Memory normalization
and presentation fail through their existing unavailable-error contract with
`local_gpu_busy`, before contacting the model. Embeddings, CPU audio, file work
and web research are outside this generation-only admission protocol.

This requires a coordinated rollout of the compatible API, image worker and
Studio. Stage the three candidates first. At idle, stop the image worker,
activate the API lock path and worker opt-in as a pair, switch compatible code,
then restart the worker. Enabling the API path while a legacy worker holds the
lock before claiming would block that worker's own claim. Rollback likewise
restores a compatible configuration pair while the worker is stopped; preserve
the lock inode and any unresolved marker. With the worker opt-in omitted or `0`,
its legacy preclaim-lock behavior remains; with no API path, host admission is
disabled in the API. CPU audio ignores the image-worker opt-in. These candidates
and settings do not deploy themselves, unload models or prove GPU residency has
ended after a model HTTP response.

Readiness distinguishes a GPU reservation from a probe or model failure through `availability_code`, alongside the existing `ready` and `message` fields. An Ollama model with explicit integer `size_vram: 0` is CPU-only and does not alone block Studio; any positive value reserves the GPU, while missing, malformed, or unavailable VRAM information fails closed. Active Swarmer calls/jobs and the shared image lock still block admission regardless of residency. Studio never unloads a model. The UI polls every two seconds while unavailable, generating, or disconnected, and every six seconds while idle and ready. Its header and explanatory message identify the actual readiness reason rather than treating every unavailable state as GPU usage.

Run the focused checks with `server/.venv/bin/python -m pytest -q tools/chroma-studio/tests` and `node --test tools/chroma-studio/tests/test_ui.cjs`. The UI tests execute the real script with a minimal DOM and mocked status replies; they do not replace on-device browser qualification.

The earlier shared-lock Studio release was deployed after the active user generation finished, with the private binding and configuration preserved. See its [historical deployment receipt](../../docs/evidence/chroma-studio-2026-10-02/shared-gpu-lock-deployment.json). That receipt does not qualify the coordinated API admission and durable-marker changes described above; those require a fresh deployment receipt and runtime verification.
