# ANEMLL reference: four individual iPhone loads

On 4 October 2026, all four reference load contracts succeeded in a standalone UIKit diagnostic on the iPhone, using `MLModel.init` with `cpuAndNeuralEngine`. This establishes individual loading compatibility for these exact artifacts. It does not establish predictions, shared-state operation, simultaneous residency, text generation, or measured ANE execution.

## Pinned reference and integration boundary

The reference is [anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0](https://huggingface.co/anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0/tree/c6461a77a6f803424ec347f9537aadac37094879), revision `c6461a77a6f803424ec347f9537aadac37094879`. Its 20 retained files total 1,073,761,040 bytes. Public LFS SHA-256/Git-blob identifiers were independently verified; no conversion or weight modification occurred.

Three precompiled `.mlmodelc` directories contain 15 files, totaling 1,064,616,175 bytes. The explicit load contract is:

| Component | Function | Device load time |
| --- | --- | ---: |
| `llama_embeddings_lut8.mlmodelc` | default | 12,034.249 ms |
| `llama_lm_head_lut8.mlmodelc` | default | 4,476.218 ms |
| `llama_FFN_PF_lut4_chunk_01of01.mlmodelc` | `infer` | 12,316.890 ms |
| Same FFN component | `prefill` | 13,534.073 ms |

The descriptor resolves the absent `meta.yaml` alias `llama_FFN_PF_lut4.mlmodelc` to the existing chunk without rewriting metadata. Artifact declarations are ANEMLL 0.3.0, coremltools 8.2, specification 9 and iOS 18 minimum.

Graph context is **512**, fixed prefill batch **64**. FFN's single state is `model_model_kv_cache_0`, FP16 `[32,8,512,64]` (16 MiB theoretical); the head emits eight logits partitions `[1,1,16032]`. Tokenizer metadata does not enlarge graph context. No state or prediction contract was exercised.

At source snapshot `0299efce03199d4ded2e82c43d4a65edef9a4812`, monGARS' [HF resolver](../../mobile/src/lib/hugging-face-models.ts) selects `.mlpackage`; [LocalModelStore](../../mobile/modules/swarmer-local-inference/ios/LocalModelStore.swift) requires one model. [CoreMLRuntime](../../mobile/modules/swarmer-local-inference/ios/CoreMLRuntime.swift) expects token IDs to one `logits` output and two key/value states when stateful. This three-component, multifunction reference needs a dedicated adapter. The diagnostic excludes monGARS/Expo integration.

## Observed attempts and limits

The first launch, `72a26eac1e6844ad954a599f1aa1e164`, reported process startup but produced no native load receipt; its own Documents listing contained zero files. Its cause remains unknown. It supplies neither a successful load nor a Core ML load failure diagnosis.

Build 02 added bounded lifecycle checkpoints while preserving the exact activation guard and unchanged Core ML runner. Build and installation exited 0. Its IPA SHA-256 is `892d02cc18433d07ca8005325a0c1c65c708724a4699b2ffed1daf01cb335926`. Run `fa836e6ff51e44ce90d80b9594370287` confirmed accepted arguments and runner entry, then completed between **06:51:32 and 06:52:16 UTC** on device-reported iOS 26.7 (23H24).

The final receipt reports `loaded_all`, `complete`, no errors, and 15 files verified before/after. Models were released between loads. It explicitly records no state creation, predictions, compute plan or hardware measurement. CPU+ANE configuration does not prove ANE execution. Single observations supply no latency distribution or explanation for the [synthetic two-block load failures](coreml-cache2-direct-load-2026-10-04.md).

## Retained evidence

Private roots under `/Users/ales27pm/Library/Logs/SwarmerQualification/CoreML/`:

- `anemll-llama1b-reference-20261004/`: `files-manifest.json`, `reference-descriptor.json`, `ready-receipt.json`, and `independent-review/artifact-receipt.json`.
- `anemll-standalone-load-20261004/`: preparation/build/install/IPA receipts, source files, both run directories, and the final `run-fa836e6ff51e44ce90d80b9594370287/device-receipt-final.json`.

Independent read-only review confirmed digest, four paths/functions/order, errors, revision/manifest bindings and execution limits, without device commands or reruns.

| Evidence | SHA-256 |
| --- | --- |
| Source file manifest | `dece052e95e3768963518ced26ef3a06d13e5198d7be10dead1732b86a8a5333` |
| Bundled resource manifest | `94cad31ae1983bdd53cbad6272617012c8e749944c1ea87cbbabc0342ee94990` |
| Final device receipt | `cddecbbc6c474d341788edb54177cec31139ba267f1620a8344212f3c83a5812` |

Next gate: adapter and prediction/shared-state qualification. No production setting or end-to-end generation path is qualified by these loads.
