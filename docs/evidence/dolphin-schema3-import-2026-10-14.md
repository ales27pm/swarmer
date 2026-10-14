# Dolphin validation — 2026-10-14

## Verified baseline and scope

Fresh clean checkouts fetched successfully using authorized GitHub access:
- swarmer `fix/dolphin-coreml-audit-20261014`, initial `f18d1c63f56041cc5e01ddc53f6a878ffb63479c`.
- CoreML-DOLPHIN3.0 `fix/dolphin-provenance-audit-20261014`, initial `6eb4c21369babf5768c96bd35b926f5606df4e28`.

`/workspace/ales27pm` was inspected first: profile README only, clean repository; no deployment or SSH configuration. CoreML root AGENTS.md read and required compileall run. No applicable Swarmer AGENTS.md in modified directories. Existing files preserved. No main merge, workflow dispatch, deployment or phone installation.

## Defect and fix

The production exporter emitted `format_version: 3`, but `DolphinCoreMLBundle.validate` accepted only 2. This is reproduced by compiling the baseline reader and passing it the exact synthetic v3 fixture: exit 1, `manifest.json describes an unsupported Dolphin pipeline` (`baseline-schema3.log`).

Swarmer now accepts 2/3, and for 3 requires exact declared source model, revision, and typed source identity (both shards, all seven metadata records, their lengths/hashes and identity digest). Unknown versions and modified identities fail. It remains `dolphin-split9-experimental`. This is validation of a declaration, NOT authentication of converted model bytes. The existing import pipeline copies the manifest unchanged and does not copy the release evidence directory. Independent toolkit verification must therefore remain separate. Extended iOS store tests cover v2/v3 and byte preservation, but were NOT executed on Apple here.

The exporter manifest construction was extracted unchanged into `build_manifest`, used both by production export and the new reproducible `validate_import_contract.py`. The cross-repository probe compiles the actual Swift bundle reader with Swift 6 strict concurrency and warnings-as-errors. It ZIPs/extracts synthetic payloads, then checks 18 positive/negative cases. It does not execute `LocalModelStore`, model conversion or inference.

## Provenance contract

Schema 3 binds source repo `dphn/Dolphin3.0-Llama3.2-3B`, revision `392a6f57223e7ccfe6ef4ebdb2ff101a42d57364`, two source shards and seven metadata/tokenizer files. SHA-256 of the sorted compact JSON identity (before adding the digest) is `c8deb65551392cfd3f87113e0a0cf3486af96c18e551e5aa7d219d90c8c6d5b1`.

`download_source.verify_pinned_source_snapshot` rehashes actual source bytes, verifies the pinned inventory/index and captures filesystem mutation evidence. Export stages, references and verifier bind that identity. Release packaging requires schema 3 and equal fresh identities across manifest/reference/verification, serialized weight parity and numerical gate. It rehashes ZIP package inventories, sidecars, references, fixtures, source reports and metadata; duplicate members and mutations with valid CRC fail. This provides local integrity binding, not publisher signatures or Apple execution evidence. Historical checked-in manifests/references remain schema 2; no full schema 3 export is present in these checkouts.

## Executed commands and results

Environment: Debian 13 x86_64 container `758c7bfaecba`, Python 3.12.14, Node 24.19.0, downloaded official Swift 6.0.3 Linux toolchain. Python dependencies installed from toolkit requirements; torch 2.5.0 CPU. JS install used `npm ci --ignore-scripts --cache /tmp/dolphin-npm-cache`.

From `/workspace/swarmer/mobile`:

```sh
npm test -- --runInBand
```

84 suites / **1668 tests passed**, `js-tests.log`. These include the 32-token qualification limits on the model/goal screens. This is not evidence of a universal native API token ceiling or device behavior.

From `/workspace/CoreML-DOLPHIN3.0/ane-lut6-c2048/toolkit`:

```sh
/workspace/dolphin-venv/bin/python -m pytest tests -q \
  --junitxml=/workspace/evidence/dolphin-validation/python-tests-final.xml
/workspace/dolphin-venv/bin/python validate_import_contract.py \
  --swarmer /workspace/swarmer \
  --swiftc /workspace/swift-toolchain/usr/bin/swiftc \
  --output /workspace/evidence/dolphin-validation/import-contract-final
```

**121 Python tests passed**, one Core ML output-renaming warning; log and JUnit preserved. Includes real miniature Core ML serialization/compression tests and release corruption tests; NOT a full-size Dolphin export. **18 cross-repository cases passed**, `import-contract-final/results.json`; compiler command and scope are recorded there, compiler output in `compile.log`. Synthetic fixture/ZIP retained and explicitly labeled.

From the CoreML repository root:

```sh
/workspace/dolphin-venv/bin/python -m compileall \
  dolphin2coreml_full.py Sources/App/LLM Sources/App/Bench \
  ane-lut6-c2048/toolkit/run_export.py \
  ane-lut6-c2048/toolkit/validate_import_contract.py
```

Passed (`compileall.log`). Both repositories pass `git diff --check`.

## Exact blockers and access outcomes

- Full real-model export/reference/release/import: **not completed**. Source shards (6,425,541,336 bytes), full tokenizer and model packages absent. HTTP read of the published Hugging Face manifest failed with `ProxyError: Tunnel connection failed: 403 Forbidden`; remote current schema not established, no bypass attempted (`published-manifest-check.json`).
- Downloader with source `/workspace/dolphin-real/source` and partials `/tmp/dolphin-source-partials`: **failed before networking**, different filesystems prevented required atomic rename (`download-source.log`).
- Downloader with partials `/home/agent/.cache/dolphin3-ane-download-392a6f`, requested scoped write access: **failed before networking**, OS `Errno 30 Read-only file system` (`download-source-disk.log`). No automatic approval rejection was returned. Need a writable disk-backed cache on the same filesystem, outside the synchronized checkout, and authorized Hugging Face connectivity or supplied verified artifacts.
- Apple environment read: **unavailable**, `MCP SSE probe returned 429 from openai.org` (`mac-environment.json`). No Swift Apple tests or unsigned iOS build was rerun; the previous claimed build remains unverified here. LocalModelStore imports Darwin/CryptoKit and cannot be tested by this standalone Linux harness.
- Ubuntu→iMac route: current host is Debian container, not an identified Ubuntu machine. No SSH environment variables or `/home/agent/.ssh/config`; system config has only generic host defaults/systemd proxy include. Available repository docs describe Tailscale/IPv6 and historical Intel iMac validation, but no actionable SSH target/user/port/jump route. `/root/.ssh/config` stat returned **Permission denied**, not read or retried. No remote SSH attempted and no keys/settings changed. Need the registered Ubuntu environment or an authorized existing SSH alias/target and credential availability. The parent-reported offline/task-disabled iMac state was not bypassed.

## Remaining physical qualification prerequisites

1. Recover/produce the actual schema 3 export using pinned source bytes; independent reference + verifier pass, release ZIP hash verification and unchanged import bytes. Do not relabel historical v2 artifacts.
2. Authorized available Apple build host with Xcode/SDK and macOS 15+ / iOS 18+ support; rerun native store tests, Swift runtime tests and unsigned build on these exact commits. Identify VM/iMac and SSH route separately; no evidence of either access here.
3. For a later explicitly authorized phone step: suitable signed app, provisioning/trusted pairing and Developer Mode, target OS/device identity and sufficient disk/RAM. Build success alone does not establish installation or inference.
4. Run exact model/tokenizer fixtures through compilation/loading, stateful predictions, reset/cancel/context boundaries, next-token parity and qualified 32-token paths. Preserve model/artifact/build hashes and reports.
5. Inspect MLComputePlan and separately record Instruments/Core ML execution tracing for ANE claims. Measure latency, peak memory, thermal behavior and energy on the actual phone. Provisional logit tolerance 1.0 and three next-token probes are not general quality/performance qualification.

No new build installed on iPhone in this task. Complete real-model export/import and physical qualification remain blocked as above.
