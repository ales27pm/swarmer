# N02: sequential attention with separate cache states

The next diagnostic keeps the sequential two-block computation, weights, cache
shapes and slice indices (0 and 1), but gives the second block its own key/value
states. There are four state tensors inside **one MLState container**, not two
independent MLState objects. It isolates state sharing from the already tested
change to independent block inputs.

Only six second-block state references changed in the graph, with two state
descriptions and two function inputs added. Inverting these edits reproduces the
source using deterministic protobuf serialization; raw protobuf byte ordering
is not claimed to match. Weights and other package files are unchanged.

The logical FP16 state size rises from 224 MiB to **448 MiB**. On the Mac CPU,
prefill, cached decode and reset/full-sequence outputs match the reference with
zero maximum difference. Cache-loss controls fail as expected for both blocks.
Validation took 8.882 seconds. Process-group peak RSS was 305,416 KiB, excluding
Core ML services outside that group. These are local synthetic results, not a
full Dolphin or iPhone Neural Engine qualification.

The DEBUG app catalog accepts the eleventh fixture and shares its bounded native
validation with a standalone Swift test. The Release catalog stays empty.
Existing per-package, element, step and timeout limits are unchanged. The private
bundle composition cap is 96 MiB; v7 contains 92,272,615 bytes, and the ten prior
fixtures are preserved. No full-model registry entry changed.

Validation on the frozen seven-file mobile change:

- 112 Jest tests across the probe and public API contracts passed.
- TypeScript and targeted ESLint passed.
- Swift 6 strict-concurrency tests passed in DEBUG and Release.
- Independent review found no concrete defect and matched the source and bundle
  pins. The new Swift file must appear in the generated native build target.

The signed Debug app is installed on the physical iPhone. The completed target
matrix below passes CPU and CPU + GPU, but fails CPU + Neural Engine at load
with execution-plan error -14. Selecting CPU + Neural Engine does not by itself
prove hardware execution on the Neural Engine.

Private evidence:
`Library/Logs/SwarmerQualification/CoreML/two-attention-separated-states-20261004/`.

| Artifact | SHA256 |
| --- | --- |
| Model handoff | `df316e4022dd1ac02a254533902a09b9336f9091529ef55b9fc09b0d95428485` |
| Model protobuf | `d9ed8919c347f9cb4d2dd20f0c14ca67ef64d60b232f442e1f84e2ceb6896c1e` |
| Added fixture | `e3409ea033e85c47ba5ec8e1db121c2a8ecd29cdc8b104a1788502b05e34e8b5` |
| v7 manifest | `50275d01f82cb823b21f6fb3d8a72a2bf1c551b8ac07b6650a15df7c0d34e019` |
| Mobile freeze | `24388e0e9d682ad9db9f605270644b20b376289c322b057b9c6b11aa9d9ff486` |

## Signed build and installation, 4 October 2026 UTC

The app from `a85df77f3c66bc816ed35d753b6a9630e895240a` built successfully
in 1,674.78 seconds. The earlier attempt stopped on a copied precompiled-header
cache that referenced its original absolute location. Only the new task's
derived build directory was replaced for the successful attempt; the prior
signed app was preserved.

Independent verification matched all 274 tracked mobile source files, the three
dependency lockfiles, the 103 files in fixture bundle v7 and all eleven fixture
identifiers in the JavaScript/native app. Its 184-file manifest remained stable,
and deep/strict signature verification passed. No dependency upgrade or full
Release/TestFlight build occurred. The display version remains
`0.1.0 (20260930031000)`, so the commit and manifest identify this artifact.

After refreshing the Mac's user CoreDevice service, fresh device details showed
the paired iPhone 16 Pro connected over the local-network tunnel with developer
services available. `devicectl device install app` succeeded in 12.703 seconds
at 05:27:21 UTC. This updated the existing app without an uninstall. Two local
attempts to read the verifier's private directory had failed before invoking
the installer; correcting ownership allowed the single device installation.

The new IPv6 API session returned foreground health with zero active jobs. Its
catalog advertised fixture 11, and `models.status` returned idle. The subsequent
CPU probe command returned an HTTPS error with no result receipt. Its native
outcome is **unknown**; neither successful execution nor non-execution is proved.
It was not resent, and the GPU/Neural Engine cases were not sent. Device commands
were suspended while the user switched to voice mode. No new Core ML numerical
result, execution-plan diagnosis or ANE activity is claimed for this fixture.

| Receipt | SHA256 |
| --- | --- |
| Signed app manifest | `d8b12b4d327ad513a9a6e602b568d1d80752d8485f2e70c90f0212a24f9d1d9f` |
| Independent product verification | `b4dee8ed20e82c96219260d5b12f597edf3878cea4e5a6b9d7494708b3783ef7` |
| Installation result | `f80926699d724812d72421da4d022cb32659ed13fbc411866aef38c914ec447d` |
| Device catalog | `45524b0a363c0ac9fd6e40c162f916793c52daa6610f67a20094f464fcf21e9a` |
| Model status | `08ea3a30a30d4fc62171d4afa90e47b4e8ca8c1836ccd4bc26831eda03e2749f` |
| CPU command exit receipt | `62cddb959b20367a4eb85247f81674b9e8a427c09887366fbde28a30fc335c66` |

Private evidence is under `app-build-a85df77/` and
`device-a85df77-20261004/` within the qualification directory above. Session
credentials and device identifiers are excluded from this report and Git.

## Resumed physical-device matrix, 05:48–05:50 UTC

After the user resumed voice mode, fresh CoreDevice inspection found the same
iPhone available over a changed IPv6 tunnel. Foreground activation without
terminating the app succeeded, but the old HTTPS session remained unreachable,
including at the fresh route with the original TLS pin and instance fence.
The first CPU attempt remains unknown; its receipt was not overwritten or
reclassified. A newly launched API session started with zero active/retained
jobs, and each case below was submitted once with a new idempotency key.

All three API jobs completed and returned native reports. API job success means
that the diagnostic returned a report, **not** that the tested model passed.

| Fixture / requested compute units | Native result | Load (ms) | Predictions (ms) | Values compared | Maximum absolute error |
| --- | --- | ---: | ---: | ---: | ---: |
| Two sequential blocks, separate states / CPU | Passed | 204.378 | 23.822 | 61,440 | 0.0009765625 |
| Same fixture / CPU + GPU | Passed | 167.785 | 461.349 | 61,440 | 0.0001220703125 |
| Same fixture / CPU + Neural Engine | Failed at load, execution-plan -14 | 399.406 | 0 | 0 | Not available |
| Single-block slot 1 control / CPU + Neural Engine | Passed | 450.604 | 11.230 | 30,720 | 0.0009765625 |

The three-case matrix took 2.333, 2.298 and 2.272 seconds per client call,
including polling. The additional same-build control took 2.258 seconds. These
single-run synthetic timings are not a throughput benchmark for full Dolphin.
The numerical tests exercise prefill, cached decode and reset/full-sequence
predictions using the fixture's existing reference tensors and tolerances.

After the three-case matrix, health remained foreground with zero active jobs
and all three receipts retained; `models.status` returned idle. The subsequent
single-block control also completed normally. No full model was loaded, no
application rebuild was needed, and no backend state was changed by these tests.

Separating the two blocks' state tensors does not resolve this load failure.
The same-build single-block control rules out a blanket failure of this app's
CPU + Neural Engine diagnostic path. It does **not** establish whether the
remaining cause is graph composition, state allocation, a compiler limitation
or another interaction. The separate-state fixture also doubles logical state
storage, so its failure cannot isolate state sharing as the sole cause.

For the control, `MLComputePlan` reports 61 preferred Neural Engine operations,
23 CPU operations and 120 unknown assignments. These are plan descriptions,
not execution telemetry; `hardwareExecutionMeasured` remains false throughout.
Full Dolphin correctness and Neural Engine execution remain unqualified.

Private evidence: `device-a85df77-voice-resume-20261004-01/` beneath the same
qualification directory. The prior attempt remains in its original directory.

| Receipt | SHA256 |
| --- | --- |
| Three-case matrix | `18e9b0c79362dfaf7e8c88964dd72395798bb47d14eeb0173cce61ccdd3c9b9e` |
| CPU result | `442162eb8e4e67f07bc5a9a884b00f0b3052ea2619034d9e1ac087fbba3b42a2` |
| CPU + GPU result | `3d6914961a0152bedfdd8c11244938b44311e99d7d73f0f9308762c2d1497269` |
| CPU + Neural Engine result | `fdabe4246bc2c3248a09794931c5ac9980e2ff15db5a14b56ec3e56c89996698` |
| Single-block slot 1 control | `93709d0e9656de14ab2e3563760b4af544b37037e942dfa3d8ec2dc8b08cf307` |
