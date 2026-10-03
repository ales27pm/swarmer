# Chroma Studio delivery — 2026-10-02

## Scope

A standalone French web interface for the already-qualified local Chroma1-HD
runtime on Ubuntu. Prompt, negative prompt, seed, 1–40 steps, fixed 512 × 512
output, cancellation, persistent completed-image history and PNG download.
Rendering happens on Ubuntu; the browser polls job state and can reconnect.

Initial immutable release: `20261002-f491e420363b`; exact hashes in `release.json`.
Only `tools/chroma-studio` was packaged. Existing dirty iOS/server changes were
not deployed. The separate Swarmer media integration remains source-only.

## Actual network deployment

- URL: `http://100.125.44.127:8765/`.
- User service: `chroma-studio.service`, enabled for the Ubuntu user.
- Uvicorn listens exclusively on `100.125.44.127:8765`, not the LAN or wildcard.
- Access requires a Tailscale peer permitted by the tailnet ACL. This is HTTP
  carried inside the encrypted Tailscale network, not HTTPS.
- Serve on HTTPS 8446 was attempted but required unavailable administrator
  authentication. No privileged setting was changed. A user-service override
  supplies the private Tailscale binding and exact Host/Origin allowlists.
- Existing Tailscale Serve/Funnel configuration was compared before/after and
  remained identical. No public Funnel was enabled for the studio.
- The studio has no additional per-user login; allowed tailnet peers can see
  its history. Same-origin/CSRF/Host protections guard browser mutations.

Python 3.12.13 has a dedicated environment under
`~/.local/share/chroma-studio/venv`. Model and runtime mounts are read-only in
the renderer container. The renderer has no network, a 12 GiB memory limit,
6 CPU quota, 4 native threads and a 900-second deadline. GPU admission checks
production jobs and Ollama; it yields by cancelling only its own container.
This is a polling safeguard, not an atomic shared GPU reservation.

## Browser and runtime qualification

The real IAB browser on the Mac accessed the private Ubuntu address.

- Initial page showed an explicitly labelled prior Chroma example, no invented
  history. No browser console errors were reported.
- A real prompt launched job `07888fd026964d9a8711ade9f52b73b3`.
  Cancellation from the browser completed with status `cancelled`, 17.07 seconds
  total elapsed, and no generated image. A new job was subsequently admitted.
- Full-render job: `5d6854b8d9db400aa5d586a2ce6b3264`, landscape/cabin prompt,
  seed 42, 40 steps, 512 × 512. Completion evidence is recorded separately below.
- Reloading the browser during this job recovered the same active job and
  preserved the prompt draft; it did not submit another generation.
- The 390 × 844 responsive check showed a single column without horizontal
  overflow (document width 375 px within a 390 px viewport). This is a browser
  viewport check, not physical iPhone proof.
- Live rendering exposed a Docker logging delay: carriage-return-only sampler
  counters reached `docker attach` immediately but remained buffered in
  `docker logs`. The follow-up runner patch changes only log observation and
  is qualified separately from the initial full render.

## Visual fidelity ledger

Reference: `concept.png`. Actual running screenshots: `desktop-running.png` and
`mobile-running.png`.

1. **Composition:** prompt and controls left, square preview right; a single
   column below 680 px. Recent images occupy a separate full-width row.
2. **Palette:** the reference's near-white, navy and indigo hierarchy is retained.
3. **Typography:** bold compact headings, quieter labels and helper text; system
   sans-serif fallback avoids a remote font dependency.
4. **Controls:** prompt, seed, steps and download match the reference hierarchy.
   Negative prompt and cancellation are intentional functional additions.
5. **Honest states:** prior example is labelled, empty history stays empty, actual
   progress is measured. No fabricated thumbnails or progress percentages.

Copy changes are intentional: French operational status, 40-step time estimate,
optional negative prompt, fixed size, cancellation messages, private/local footer
and precise backend-unavailable explanations replace decorative mockup copy.

## Separate Swarmer integration

Server contracts and planner support `model_profile: chroma1-hd-q4`, fixed 40-step
512 × 512 jobs, and existing owner-authorized binary artifact delivery. The media
worker supports the pinned native runtime/model recipe, literal prompt arguments,
PNG validation, process-group cancellation and a 600-second image deadline.

Local verification: 143 server tests, 41 mobile tests, 82 media-worker tests and
9 server/worker invalid-payload parity cases passed. These do not prove deployed
Swarmer generation. The production worker, dispatch registration and iPhone path
have not been activated/qualified as part of this standalone studio delivery.
Studio jobs currently have their own history; they are not Swarmer goal jobs.


## Completed full-render result

`full-render-result.json` and `cabin-receipt.json` confirm the browser-submitted
40-step image completed in **251.585 seconds**, exit 0, no OOM. Peak whole-card
VRAM was **6,202 MiB**. The downloaded PNG is 512 × 512 and 616,931 bytes, SHA-256
`931f384f1259b4e1aea3d2912a90efe3f778d4b8cd19af3d85dcd5f4a87af867`.
The download endpoint returned HTTP 200, `image/png`, an attachment filename,
no-store, nosniff and the intended same-origin security headers. The actual
image was visually inspected and matches the cabin/lake prompt.

## Progress follow-up release

`20261002-7de1a188dc5d` was activated only after the active user generation ended.
Its exact archive and per-file hashes are in `progress-release.json`. Only the
runner's log transport and deployment documentation differ from the initial
release; frontend/model/runtime/settings are unchanged. **36 backend tests**,
Ruff and diff checks passed. All six existing Swarmer services and the new
studio service are active. Studio history survived this restart.


The real follow-up browser job `a8fa30f8c6da4f82a83104a32e16ed74` (origami boat,
4 steps) completed in **106.86 seconds**. Polling captured **25% and 50% while
status was still running**; see `progress-samples.jsonl`. The short run tests the
transport, not default image quality. Selecting the cabin in history restored
its original prompt, seed and 40-step setting. Clicking its browser Download
link produced a file byte-identical to the previously validated PNG.

Final browser screenshot: `studio-ready.jpg`. The frame excludes the history
row to avoid including other user-created images. Temporary viewport overrides
were reset and the studio tab was retained for the user. No physical iPhone
browser test was performed by the agent.
