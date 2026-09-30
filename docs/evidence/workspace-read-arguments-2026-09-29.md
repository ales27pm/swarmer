# Structured workspace reads — 2026-09-29

Status: implemented locally. Production deployment and live model qualification are not established by these checks.

## Defect and change

The planner/evaluator generation schema previously forced `worker_arguments: null` for `workspace.read_text`, although dispatch needs an explicit path. A read now has a dedicated worker branch requiring exactly `worker_arguments.path`. It is excluded from the generic null-arguments branch and is only advertised when the skill is available (or availability is unknown under the existing evaluator contract).

Both model providers use the same branch. Their instructions distinguish an explicit reader-workspace path from a project draft manifest. There is no new user-facing form instruction and no direct filesystem access for the model.

Existing server validation, permission policy, persisted node metadata, dispatcher and file worker are reused. No change is made to the project worker's `focus_paths` mechanism. A narration saying that a read will happen is not execution evidence.

## Adaptation of the supplied kit

The ZIP checksums were verified. Its 29 isolated tests pass. Its preview command correctly refused to patch locally modified files, so the changes were adapted against snapshots of the current files instead of forcing `--apply`.

The dedicated argument schema goes through the repository's `model_wire_schema` normalization. This omits string `maxLength` in the Ollama grammar to avoid expanded llama.cpp repetitions. The actual 500-character path limit is still enforced by public proposal validation and again by dispatch; tests cover both boundaries. `path` remains required and nonempty in the generation schema. Unsupported pagination and capability fields remain forbidden there.

Existing evaluator fixtures now provide explicit read arguments. Fixtures that change a read into a different skill also clear those arguments, preserving the intended schema/graph diagnostic assertions.

## Local execution evidence

New integration tests use a private SQLite database, the real dispatcher and the real file worker implementation against a temporary workspace. They establish that:

- The explicit path survives persistence and reaches the claimed job unchanged.
- The downstream writer cannot run before a valid read result is accepted.
- Actual file content reaches the writer with the read node ID, worker job ID and untrusted-content classification.
- A draft-only file is not found under the reader root, and the existing draft file remains unchanged.
- An announced read without a content result does not satisfy the dependency.
- A result submitted with the wrong lease generation cannot release the writer.

These tests do not invoke Ollama, cross a production HTTP connection, run an iPhone flow or prove persistent semantic-memory ingestion. Dependency context remains bounded by the existing handoff limits; this fix does not promise that an arbitrarily large read is supplied in full to a later model.

## Verification receipts

The verification environment, original file snapshots, turn-specific patch, source hashes and logs are retained under `/Users/ales27pm/Library/Logs/SwarmerQualification/workspace-read-fix-fxhe16tf/`. The environment uses Python 3.12.13 and the server's declared development/optional dependencies; its resolved versions are recorded in `test-environment.txt`.

Results on the local integrated worktree:

- Full server invocation: **3,123 passed, 11 skipped, 2 failed** in 540.46 seconds. Both failures were old test fixtures affected by the explicit read-argument requirement: the evaluator graph-diagnostic fixture and the mixed-project generation-schema fixture. They were corrected without changing production code; the exact two failing tests then both pass. The entire suite was not rerun after these fixture-only repairs.
- Targeted schema/provider/evaluator/handoff/research-wire/recovery run: **244 passed**. Mixed-project grammar suite after its fixture repair: **24 passed**. These overlap the full invocation and are not additional distinct server tests.
- Existing standalone file-worker suite, run without root privileges: **30 passed**.
- Supplied kit's isolated tests: **29 passed**. Four targeted assertions were observed failing before the application fix (valid paths rejected and null accepted by planner/evaluator grammar).
- Ruff lint/format checks on all eight changed Python files, mypy on the two provider modules, and `git diff --check`: passed.

The production code remained unchanged throughout the full-suite run and the fixture repairs. Skipped integration tests remain unqualified; these local results do not substitute for production or device evidence.
