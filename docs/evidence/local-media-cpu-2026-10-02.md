# Local French speech qualification — 2026-10-02

This run used an isolated directory and Python environment on Ubuntu. No
production service, worker, database or iPhone application was modified by the
qualification. All model files were verified against pinned Hugging Face
revision `f3ff3571791e39611d31c381e3a41a3af07b4987` of `hexgrad/Kokoro-82M`.

## Runtime

- Python 3.12.13; PyTorch 2.8.0+cpu; Kokoro 0.9.4; Misaki 0.9.4.
- `espeakng-loader` 0.2.4; Transformers 4.51.3; NumPy 2.2.6.
- French voice `ff_siwis`, CPU, two Torch threads; no GPU used.
- Local weights/configuration only, offline flags and Python connection blocking.
- Full dependency freeze and SHA-256 receipts are retained with the run.
- `uv pip check` checked all 91 installed packages: no dependency conflict.

## Direct model smoke

| Sample | Audio duration | Synthesis elapsed | Real-time factor |
| --- | ---: | ---: | ---: |
| Standard French sentence | 5.65 s | 2.410 s | 0.426 |
| Familiar French sentence | 5.45 s | 2.099 s | 0.385 |

Model loading took 0.837 s; peak process RSS was 1,301,404 KiB. These are two
small samples, not a throughput benchmark. Both files decoded as finite,
nonempty 24 kHz mono PCM16 WAV. Listening and transcription were not validated.

## Real worker render path

`MediaRenderer.generate` was executed with the installed profile, its own
subprocess and the real Kokoro backend. `validate_media` then checked the output.

- Normal request: 5,650 ms audio, 271,244 bytes, 6.025 s total including process
  startup, integrity checking, imports, model load and synthesis.
- SHA-256: `7989730cfd60d285e239462c3095f53171dd37bdfdbee50d62dd99b0f63289d7`.
- Same text with a one-second limit: explicit `duration_limit`, 5.398 s;
  no partial `output.wav` remained.
- Cancellation injected through the worker's activity callback after two
  seconds: the real child process stopped at 2.028 s, with no accepted result
  and no `output.wav`. Receipt: `worker-cancel-receipt.json`.

This verifies actual synthesis and duration rejection in the worker rendering
path. It does not establish production deployment, iPhone playback,
intelligibility or an image-generation model.

## API replay with the generated WAV

The exact worker output was replayed through FastAPI with a temporary database:
goal creation, explicit manual plan, worker claim, binary upload, result
acceptance, listing and authenticated download. Upload, result acceptance and
download returned HTTP 200; the 271,244 bytes and their SHA-256 were unchanged.
Before result acceptance, the uploaded file was absent from the published list.
Anonymous download returned 401 and a different paired owner received 404.

Receipt: `api-real-audio-receipt.json`. This was an isolated API test, without
an LLM planner, a production endpoint or an iPhone client.

## Final source replay

After review changes, the frozen render sources were copied to a separate
qualification directory. Their real CPU replay produced 5,650 ms of WAV in
6.057 s total; the one-second bound again returned `duration_limit` with no
partial file (5.462 s). See `worker-final-receipt.json` for the source hashes.
The final WAV SHA-256 is
`8590189eb0617d43cf90b509837120802f636c7db775b18b13249cec6c4afbee`.

The final WAV also passed the isolated API lifecycle with unchanged bytes and
the same 200/401/404 access results (`api-real-audio-final-receipt.json`). Audio
is not claimed to be bitwise reproducible across separately generated jobs.

Validation for this change: 38 new server media tests, the 287 related server
tests exercised by the parent, 14 historical-policy checks after their final
adjustment, 65 worker tests, and 105 mobile tests passed. Some server runs
overlap; these counts are not a combined total. TypeScript, ESLint, server
MyPy on 14 sources, Ruff and `git diff --check` passed. This is targeted
validation, not a claim that every repository test or a native iOS build ran.

## Receipts

Private run directory on the Mac:
`~/Library/Logs/SwarmerQualification/media-local-20261002/`.

It contains `model-manifest.json`, `smoke-receipt.json`, `worker-receipt.json`,
`python-freeze.txt`, qualification scripts and generated WAVs. The worker receipt
records the exact hashes of the three Python source files executed. Re-running
after source changes must produce a new receipt rather than overwrite these
measurements.
