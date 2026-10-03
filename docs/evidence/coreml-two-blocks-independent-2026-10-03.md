# Core ML: two blocks with independent inputs — 3 October 2026

## Result and boundary

Fixture `dolphin-attention-int4-perchannel-cache28-two-blocks-independent` is prepared and **CPU-qualified locally**. No iPhone call, full-model conversion, re-quantization or download was performed. This is not proof of Neural Engine execution or of full Dolphin correctness.

The preceding physical slot1 test already passed CPU, CPU+GPU and CPU+Neural Engine; its sequential two-block control still failed loading with −14. The five receipt hashes in `cache-slot1-20261002/device/probe-matrix.json` were rechecked successfully. That matrix is dated 2 October, not a new device test.

## One changed factor

Starting from the pinned sequential two-block protobuf, four input references change from `attentionOutput` to original `hiddenStates`: operation 217 (`shape`) and operations 236, 238 and 239 (Q/K/V `linear`). Reversing those four references restores the full deterministic protobuf serialization exactly.

Weights, package manifest, all other operations/constants, cache slice indices 0/1, two shared FP16 states `[28,1,8,2048,128]`, Q/K bounds and both output descriptions are unchanged. The model remains two copies of layer0 attention, not real layers0 and1. The package is 12,672,177 bytes and retains two SDPA operations and four state writes.

## CPU evidence

The oracle runs the original single-block package twice, with separate states and identical original inputs. Both runs match the frozen baseline. The candidate matches all six outputs across prefill4, cached-token1 and reset/full5; both cached/full comparisons pass. Maximum error is **0** throughout. Absolute and relative tolerances remain 0.005.

The unchanged sequential model also matches all its own frozen references. Its second output differs from the new oracle by up to **0.1956024169921875** (prefill/full) and **0.095672607421875** (cached), failing those same tolerances. A deliberate reset before cached decoding differs by **0.045963287353515625**, also failing. These controls establish sensitivity to input wiring and lost cache history.

Identical weights and inputs can conceal identical writes to the wrong slice, so slice retention is established structurally; no direct inspection of opaque MLState storage was attempted. The oracle uses the same Core ML CPU backend, with a different graph/state arrangement.

Runtime: coremltools 8.0, NumPy 1.26.4, Torch 2.2.2; Python `/Users/ales27pm/.cache/uv/archive-v0/iW9m-GhYyYcS6sBp/bin/python`, launched with `-I -B`. One supervised child exited 0 after 8.3692 s; peak process-group RSS 341,636 KiB. Limits: 180 s wall, 120 s CPU, 1 GiB process-group RSS, 2 library threads / 1 Torch inter-op thread. Native services outside that process group are not measured. Timings are not a benchmark.

## Files and pins

Evidence root: `/Users/ales27pm/Library/Logs/SwarmerQualification/CoreML/two-attention-independent-20261003`.

| Artifact | SHA-256 |
|---|---|
| `prepare_and_validate.py` | `f3d4befa1e594e2d78d4e5b6ea655636cd72953efb8cc3dbe01723f397b35e2b` |
| `ablation-receipt.json` | `3d1c5ef8e35e32a2c6114a8b87a3ecd836844c4121c7165d213b8f11288115ee` |
| `process-receipt.json` | `16f9ea970e3cb8a29846a4e75206274610b726c0cb386fabd217c577781a0b22` |
| `fixture/fixture.json` | `472d15398259c8361581c11407c2b7e66bc23c7e8307bf0d9e5ae31f63cb4f61` |
| Candidate `Data/com.apple.CoreML/model.mlmodel` | `00e7ad837b036968c5c8c3f0ed2a121ae4c455d4f4a42493b501e528d65fb395` |
| Unchanged `weights/weight.bin` | `d147b88bcf7defffa3d6dc75f1d8994c15eb0e967e5960b2d5203a9cd095d047` |
| `fixtures-v6/manifest.json` | `eebf6c018d6d7781d542d3c7c3b39a0ed465caa5b58bbb8ecdccd968871492f7` |
| `fixtures-v6-receipt.json` | `1f7b9d8dcfc3036afa6e0233ed22ee856be46f802eeebd1f8516afcb9fa5e24a` |
| Complete local `MANIFEST.json` | `0c0051baa46aaaafddbec91c301ff2574e121501941cdc5977ad7d9d1ec549b9` |

Builder inputs come from `../cache28-20261002/fixtures-v3` and `../cache-slot1-20261002/fixtures-v5`. New references are generated into `fixture/data/`, with 92,206 total input/output tensor elements and two outputs per step. `run_bounded.py` supervises only this new child. `compose_bundle.py` assembles `fixtures-v6`.

All **nine existing fixture objects and file bytes are preserved**. The prior bundle manifest SHA is `6ebf9298720a3dd2f8f3ffe7714173e5227f65b7e849af7177663db5507e7e29`. The ten-fixture bundle totals 79,224,197 bytes; only the private composition script's aggregate bound grows from64 to80 MiB. The native per-package limit remains32 MiB and the existing native limit already admits10 fixtures.

## Remaining device test

The mobile owner adds the tenth ID to TS/Swift DEBUG allowlists and points the generated DEBUG resource copy at `fixtures-v6`; the separate full-model candidate manifest remains unchanged. Run `models.coreml-probe` with this fixture ID for `cpuOnly`, `cpuAndNeuralEngine`, and `cpuAndGPU`, plus the existing sequential CPU+ANE control in the same build/session. Use native report outcome/stage/comparisons, not only the API job's transport state.

A success would implicate sequential dependency or compiler optimization in this fixture. Identical branches may be combined by the compiler; examine MLComputePlan counts without treating preferences as hardware telemetry. If failure persists, separate per-block states while retaining sequential inputs is a subsequent proposed isolation. Neither result proves full-model ANE readiness.
