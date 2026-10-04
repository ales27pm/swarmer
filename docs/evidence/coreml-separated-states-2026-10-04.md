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

An app build, installation and target-device matrix remain to be performed for
this fixture. Selecting CPU + Neural Engine will not by itself prove hardware
execution on the Neural Engine.

Private evidence:
`Library/Logs/SwarmerQualification/CoreML/two-attention-separated-states-20261004/`.

| Artifact | SHA256 |
| --- | --- |
| Model handoff | `df316e4022dd1ac02a254533902a09b9336f9091529ef55b9fc09b0d95428485` |
| Model protobuf | `d9ed8919c347f9cb4d2dd20f0c14ca67ef64d60b232f442e1f84e2ceb6896c1e` |
| Added fixture | `e3409ea033e85c47ba5ec8e1db121c2a8ecd29cdc8b104a1788502b05e34e8b5` |
| v7 manifest | `50275d01f82cb823b21f6fb3d8a72a2bf1c551b8ac07b6650a15df7c0d34e019` |
| Mobile freeze | `24388e0e9d682ad9db9f605270644b20b376289c322b057b9c6b11aa9d9ff486` |
