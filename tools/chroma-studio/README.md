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

Generation uses fixed 512 × 512 dimensions, one image per job, 1–40 steps (40 by default), a user seed or a generated seed, and an optional negative prompt. The qualified Euler/CFG/Flux/attention and GPU-placement settings are not browser-controlled. Each Docker renderer is isolated, has no network, and has a 15-minute deadline and a 12 GiB memory limit. A single active studio job is allowed. Studio and the media worker share an atomic image-generation lock, described below. The service also checks other production GPU work and Ollama residency before and during rendering, yielding to Swarmer by cancelling only its own container. Those existing code/model checks remain polling guards, not a global GPU reservation.

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

Studio and the new image worker coordinate through a nonblocking exclusive POSIX `flock` on `~/.local/state/swarmer-gpu/image-generation.lock`. Studio acquires it before recording a queued job and holds it through renderer cleanup and the terminal receipt; the worker acquires it before claiming a job and holds it through result delivery. Neither process unlinks the lock file. `CHROMA_STUDIO_GPU_LOCK` can override the Studio path, but both services must use the same inode and compatible file permissions. An occupied lock makes Studio unavailable and rejects a new generation with HTTP 409; an inaccessible lock returns HTTP 503. Unconfirmed cleanup retains the Studio lease and blocks admission until recovery. The worker also checks for orphaned Studio containers because a process crash releases its file locks while a detached renderer may survive. Other code/model GPU activity continues to use the existing read-only polling checks.

Readiness distinguishes a GPU reservation from a probe or model failure through `availability_code`, alongside the existing `ready` and `message` fields. An Ollama model with explicit integer `size_vram: 0` is CPU-only and does not alone block Studio; any positive value reserves the GPU, while missing, malformed, or unavailable VRAM information fails closed. Active Swarmer calls/jobs and the shared image lock still block admission regardless of residency. Studio never unloads a model. The UI polls every two seconds while unavailable, generating, or disconnected, and every six seconds while idle and ready. Its header and explanatory message identify the actual readiness reason rather than treating every unavailable state as GPU usage.

Run the focused checks with `server/.venv/bin/python -m pytest -q tools/chroma-studio/tests` and `node --test tools/chroma-studio/tests/test_ui.cjs`. The UI tests execute the real script with a minimal DOM and mocked status replies; they do not replace on-device browser qualification.

The shared-lock Studio release was deployed after the active user generation finished, with the private binding and configuration preserved. See the [deployment receipt](../../docs/evidence/chroma-studio-2026-10-02/shared-gpu-lock-deployment.json). This receipt establishes the Studio code cutover and readiness; the Swarmer media-worker deployment and its generation proof are recorded separately.
