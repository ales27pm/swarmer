# French audio deployment — 2026-10-03

Worker `media-audio-dba819494b6ad405254d` is deployed and active through
`swarmer-audio-worker.service`. Agent ID: `agt_3f2112f742c549bc9ebb352a9390ccd4`.
It uses the pinned Kokoro CPU profile and French voice ff_siwis. This is local
Ubuntu synthesis; the phone still requires a network connection to retrieve it.

Only `audio.synthesize` was added at policy epoch 9; previous rules remain
identical. The full backup retained all 10,085 context rows. A stale helper
fingerprint initially rejected newly appended copies of a context already in
that backup. A separate bounded verifier compared every original row, every
append against the exact existing reference, all 52 protected fingerprints,
and device identities. No history or failed receipt was rewritten. See
`policy-verified-independent-context.json` and the staging report.

## Real qualification

Goal `goal_83c3cc2168984eca9f8993eea19c2326` ran a supplied single-node plan
in assisted mode, with budgets 100 calls, 20 steps, 10 replans and 24 hours.
The normal worker claim, offline CPU render, upload, authenticated download and
evaluation all ran. The goal completed at `2026-10-03T08:58:40.231807Z`.
The audio node completed about eight seconds after claiming.

The WAV is 313,244 bytes, PCM16 mono 24 kHz, 6.525 seconds. SHA-256:
`3976e3e2dbbeae8fcfd37f1211d596da37bc130a69e769c750b96cae56aaded4`.
Response and artifact hashes match. Anonymous access returns 401; owner
download uses `Cache-Control: private, no-store`.

The text is: “Bonjour. Ceci est un essai de voix française locale. La synthèse
fonctionne sans connexion Internet.” The model is not a Québec-accent model.
Binary verification is not a listening-quality or transcription-equivalence
measurement. See `qualified-audio.wav`, `verified-audio.json`, `goal-outcome.json`.

## iPhone boundary

A Debug build with the native audio player was installed and launched
successfully on the physical iPhone; app.status returned active. A later
goals.create request lost HTTPS connectivity. Read-only reconciliation found
no durable creation receipt and no matching project for its unique request.
It was not resent, and device qualification is awaiting foreground access.
This run does not prove iPhone audio playback or autonomous initial planning.
