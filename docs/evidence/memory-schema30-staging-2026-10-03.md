# Versioned and symbolic memory: Ubuntu staging — 3 October 2026

The schema-30 application at `527a2380c7f57efc5a880b1392704049bd94f4d3`
and its separately qualified recovery release are now installed in private
release directories on Ubuntu. **Neither is active.** The running API still
uses `local-20261003-memory-compat27-397e979987a6` and production schema 27.

## Completed behavior and evidence

- The recovery release starts from `7dcd7f945df634a104bc6eb1a9d3ed6397dd0e36`
  plus a recorded compatibility patch. It creates through schema 29, reads
  schema 30, and transactionally removes dependent symbolic proposals when
  forgetting their source memory. It refuses unexpected symbolic schemas.
- Source archive, base archive plus patch, wheel and installed application
  contents agree. The recovery package contains 126 application files; the
  candidate contains 127. Both use the predecessor's compatible dependency
  environment. The recovery interpreter reports Python 3.12.13 on Ubuntu.
- Runtime import checks reject an editable installation shadowing the installed
  `app` package, even under Python's isolated mode.
- A fresh read-only SQLite backup from Ubuntu was migrated on private copies
  through every committed prefix: 27, 28, 29 and 30. The actual predecessor
  preserves copies at 27/28; the prepared recovery binary preserves 29/30.
  All 63 original tables and their rowids are preserved. New versioned-memory
  seed records and their sequence values are checked exactly; the six symbolic
  tables start empty. No database downgrade or backup restoration is used.
- After staging, the live symlink and live schema 27 remained unchanged. The
  existing API service was active and `/health` returned `status: ok`.

The private combined rollout suite passed **243 tests in 30.52 seconds**. This
includes migration, interrupted-prefix recovery, unknown embedding outcomes,
artifact provenance, staging, import-origin and inherited failure cases.
Targeted Ruff checks also passed. Three initial test-harness failures were fixed:
one old root-owned fixture was copied byte-for-byte into a new user-owned private
directory, and two rejection-message expectations were updated. No production
behavior or historical artifact permissions were changed for those repairs.

The application-wide result remains the separately recorded **3,939 passed,
11 skipped** at the frozen candidate. It was not repeated for private rollout
tooling or this evidence document. This staging receipt does not establish
translation quality, live retrieval quality, iPhone compatibility or ANE support.

## Artifact identities

| Artifact | SHA-256 |
| --- | --- |
| Candidate wheel | `5f4d87c0d1acdb937768fa1dea6e5161a5950ee2f468b41ab492c0542d04848a` |
| Candidate Git archive | `f99549439df9f1ecc8f55c5dd6c0617a8d37a46e07fede8bb36e71755bfe47dc` |
| Recovery wheel | `724628b14c66b9cadccebb89fb8eca9443870ee7f740843c12073ad6cecd9ed3` |
| Recovery source archive | `dabcae5eea811b53ef4037f5520977b5d44fd28edb651bac535b13523e08efe8` |
| Recovery compatibility patch | `d9c3ed8de83a7b7697d112fcade3792a6f72b4cbaf5498849f90ea0c25c0a50d` |
| Uploaded 19-file manifest | `308c844fe35b98ed7aabb1f82b858a95f1c43ac739a416195af95505345cd956` |
| Remote `stage.json` | `77dd3645b968772de01900c0b50912f0dc00fe39e5a1f5cb321d9f0cfa2964de` |
| Local completed staging receipt | `ff5dd13651c04d5b97df7e330c721875370332d2c04c23669a2130d2e7a4ff08` |

Private local evidence is under
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/rollout-schema30-20261003/`:
`completed-stage-receipt.json`, `remote-stage-summary.json`, and
`migration-proof/integrated-repaired-tests.json`. The sealed local kit contains
437 files, including 408 byte-identical historical fixture files. Ubuntu's kit
is `~/.local/share/swarmer-control-plane/rollout-kit-memory-schema30-20261003-527a2380/`.
It retains installation logs, migration/recovery copies and the complete stage
receipt. These private copies contain real application data and are not published.

## Remaining activation and product work

An installed iPhone client must accept all nine activity receipt roles before
server activation; canonical mode remaining disabled does not prevent episode
embedding receipts from using the expanded contract. Original-language display
also needs device qualification. Cutover then needs a fresh coordinated admission
baseline and the independently verified schema-30 recovery path; staging alone
does not authorize treating those steps as already completed.

The broader memory implementation still needs integration of symbolic proposals
into retrieval, native/pivot hybrid search and measured relevance, trusted lesson
promotion, and freshness evaluation. This release stores scoped evidence-backed
proposals; it does not automatically grant them policy authority.

No production qualification goal, task, image or provider call was created by
this staging work. The user's qualification cleanup remains intact.
