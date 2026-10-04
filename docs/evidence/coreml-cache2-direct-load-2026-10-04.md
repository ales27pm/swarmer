# Core ML cache-size and direct-load controls, 4 October 2026

Reducing the four state tensors' leading cache axis from 28 to 2 did not resolve
the two-block CPU + Neural Engine load failure. The synchronous Apple loader
also returned execution-plan error **-14** for both cache sizes. A single-block
slot-1 control loaded through that same synchronous path. This narrows the
diagnosis; it does not identify a unique graph, compiler or allocation cause.

The new fixture retains two sequential copies of layer-0 attention, four
separate state tensors, indices 0/1, weights and operations. It is not the real
first two layers of Dolphin. Twenty shape fields change; reversing them exactly
reconstructs the original deterministic protobuf serialization. Logical FP16
state storage falls from **448 MiB to 32 MiB**, not a measured resident-memory
reduction. The Q≤512/K≤2048 bounds remain unchanged; the three numerical
sequences exercise Q=1/4/5 and K=4/5. Eleven earlier fixtures remain unchanged.
See the [preceding separate-state experiment](coreml-separated-states-2026-10-04.md).

## Physical-device observations

Five commands completed in one app API instance, jobs 1–5, between 06:29:10 and
06:30:51 UTC. Each API job is `succeeded`: this means it returned a native
diagnostic report, **not** that the model passed. All client receipts have exit
0 and `uncertain=false`; none of these five outcomes is unknown.

| Case | Apple API / requested units | Native outcome | Load ms | Prediction ms | Values / maximum absolute error |
| --- | --- | --- | ---: | ---: | --- |
| Two blocks, cache2 | Async load / CPU | Passed | 256.730 | 21.952 | 61,440 / 0.0009765625 |
| Two blocks, cache2 | Async load / CPU + Neural Engine | Failed at load, -14 | 432.842 | 0 | 0 / unavailable |
| Two blocks, cache28 | Synchronous `MLModel.init` / CPU + Neural Engine | Failed at load, -14 | 343.775 | None attempted | None attempted |
| Two blocks, cache2 | Synchronous `MLModel.init` / CPU + Neural Engine | Failed at load, -14 | 325.716 | None attempted | None attempted |
| Single block, cache28 slot1 | Synchronous `MLModel.init` / CPU + Neural Engine | Loaded | 629.313 | None attempted | None attempted |

The direct diagnostic compiles the unchanged package, then loads it with default
configuration except `computeUnits`. It creates no `MLState`, requests no
`MLComputePlan`, and performs no prediction. Its three reported model SHA256s
match the signed resource bytes. The ordinary numerical reports identify the
fixture but contain no model hash; their binding is through the verified signed
bundle. All five reports retain `hardwareExecutionMeasured=false`.

The CPU comparison covers prefill, cached decode and reset/full-sequence outputs
against the retained reference tensors. Its compute plan reports 168 CPU and
240 unknown operation assignments, zero Neural Engine assignments. Plan
descriptions are not execution telemetry. Single observations are not a latency
benchmark; the successful slot-1 load proves neither numerical parity nor ANE
execution. Full Dolphin generation remains unqualified.

## Build, packaging and installation history

The mobile source identity is `0299efce03199d4ded2e82c43d4a65edef9a4812`.
Build 03 exited 0 after 194.017 seconds but was rejected: `ditto` failed to update
the read-only generated fixture destination. Xcode success alone did not qualify
its resources. A repair confined to the private generated build project made
copy failures fatal and adjusted only generated destination permissions; the
source fixture bundle stayed unchanged. Build 04 passed in 116.012 seconds with
the correct v8 resources.

Installing the `.app` failed in the delta installer: its manifest reported
version `0` while the installer expected `20260930031000 0.1.0`. Installing the
complete signed IPA then succeeded in **15.015 seconds**, without an uninstall.
The later five device reports establish that the diagnostic API was available;
the install wrapper's `api_ready=false` field is not a later health observation.

A subsequent read-only audit recomputed the IPA hash, all **199** archived and
retained app file hashes, and all **118** v8 resource files / **12** fixtures
(105,321,029 bytes). The display version remains `0.1.0 (20260930031000)` and is
not a unique artifact identifier. No new build, device command or model execution
was performed during this audit.

| Artifact | SHA256 |
| --- | --- |
| Signed IPA | `197a0cd63a0389f5cb1b291f3f76518f84ca14fcf94c001f05fec3b641abacb5` |
| Embedded JavaScript | `87ec3323c83f5af07b12211cadd870923e0060cdc445f3f7c4a0023b39fd66d9` |
| v8 fixture manifest | `527b863755f56c251b6327a436c8217fe733d7a6d4e16a0d8315027329cacdb1` |
| Cache2 model protobuf | `d4a9d76fd8f6e6ec09d77d0a0f3e9ed17271fa6609df9b5bc97f5196a26ef6d4` |
| Five-receipt audit summary | `636132d080fe9885f05a3784259866dad202385a216af1f812bcaae45ca5bc61` |
| Audit seal | `edf49117eb06ef3b2eef2149946075a5634b1ba620af1dc7c9e91f2bddcd2c6f` |

Private inputs and unchanged raw receipts are under
`Library/Logs/SwarmerQualification/CoreML/two-attention-cache2-20261004/`,
with the five results and `receipt-review/{summary,seal}.json` beneath
`device-0299efc-20261004/`. The seal binds input receipts, build logs, source
contracts and the verification script. Session credentials and device
identifiers are excluded from this document and Git.

The separately prepared [ANEMLL Llama 3.2 1B FAST iOS reference](https://huggingface.co/anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0/tree/c6461a77a6f803424ec347f9537aadac37094879)
is a different model and pipeline. Twenty selected files at that fixed revision
were verified against public hashes; its original `meta.yaml` alias is resolved
in a separate descriptor. This matrix neither loaded nor evaluated that
reference. Its preparation evidence is in the sibling
`anemll-llama1b-reference-20261004/`; download verification is not ANE readiness.
