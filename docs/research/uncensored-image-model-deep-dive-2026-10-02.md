# Local image models: evidence behind uncensored claims

Research date: 2026-10-02. This is a model-selection study, not an inference benchmark or deployment receipt. No image weights were downloaded and no image model was executed in this investigation. It supplements, rather than overwrites, the supplied [selection report](ubuntu-image-model-selection-2026-10-02.md) and [candidate inventory](ubuntu-image-model-candidates-2026-10-02.json).

## Decision

**Qualify Chroma1-HD first for the user's priority of broad local creative freedom.** Its publisher explicitly discloses the absence of alignment to a specific safety filter, its training lineage is documented, and a separately published GGUF Q4 variant exists. This supports a candidate selection, not a universal behavioral guarantee. The same author explicitly describes a filtered training corpus. [Pinned model card](https://huggingface.co/lodestones/Chroma1-HD/blob/0e0c60ece1e82b17cb7f77342d765ba5024c40c0/README.md)

Use Chroma1-Flash only as a subsequent speed experiment after establishing its recipe and behavior. Retain RealVisXL as a fast photorealism comparison, not as evidence of a universally unrestricted generator. Keep Qwen-Image 2.1 UC, Noct Q and Kroma as later research candidates: their dependencies, provenance questions or upstream terms make them less practical as the first production choice on this host.

## What the claim must distinguish

| Property | Evidence that would establish it | What it does not establish |
|---|---|---|
| No explicit application filter | Inspection of the exact request and result path | Coverage of every visual concept |
| No added safety alignment | Publisher's training disclosure, ideally reproducible training artifacts | An unfiltered training corpus |
| Broader generator training | Released generator deltas/checkpoints and controlled comparisons | Equal image quality or universal prompt adherence |
| Modified text encoder | Weight differences and measurements at the conditioning interface | Improved image generation merely because a chat refusal score falls |
| Runtime compatibility | Successful measured runs of the complete pinned pipeline on the target host | Compatibility inferred from a checkpoint's stored size |

The practical goal is a locally controlled pipeline with disclosed model provenance and measured behavior. The labels NSFW, uncensored, unaligned and abliterated are not interchangeable certifications. Keep original prompt, any rewriting, encoder input, generation settings and terminal outcome observable so that a lost requirement upstream is not misdiagnosed as a generator refusal.

## Candidate findings

### Chroma1-HD and Flash

HD is an 8.9B FLUX-derived base. Its author example uses 40 steps and guidance 3. The GGUF publisher offers Q4_K_M weights of 5,566,533,792 bytes. Those weights still need T5 and a compatible VAE. The model card's architectural/training discussion is stronger evidence than a marketplace label, but there is no independently verified universal-compliance result here. [HD](https://huggingface.co/lodestones/Chroma1-HD), [GGUF](https://huggingface.co/silveroxides/Chroma1-HD-GGUF)

**Flash documentation is unusually thin:** the current pinned README contains only Apache-2.0 metadata, 31 bytes. Do not infer a four-step recipe from the word Flash or copy community settings into a qualified profile without comparison. HD's card links it as a CFG-baked alternative. [Flash pinned README](https://huggingface.co/lodestones/Chroma1-Flash/blob/093c4f63b60507234aa53a073f1f9f565b3f4336/README.md)

Both full transformers have the same stored size, 17,800,038,288 bytes; faster sampling is not a smaller resident model. Their checked-in scheduler shifts differ (HD 3.0, Flash 1.0). A cached Flash GGUF listing returned by search could not be resolved through a fresh unauthenticated Hub request (HTTP 401); this does not distinguish private, gated or unavailable status, and is not a verified download plan. [HD configuration](https://huggingface.co/lodestones/Chroma1-HD/tree/0e0c60ece1e82b17cb7f77342d765ba5024c40c0), [Flash configuration](https://huggingface.co/lodestones/Chroma1-Flash/tree/093c4f63b60507234aa53a073f1f9f565b3f4336)

Newer names are not automatic upgrades. Chroma2-Kaleidoscope's WIP card identifies a FLUX.2-Klein base; Zeta-Chroma's WIP card identifies a different Z-Image-based stack. Neither card supplies HD's explicit alignment account or a comparably complete qualified recipe. Keep them experimental until their own components and behavior are established. [Kaleidoscope pinned card](https://huggingface.co/lodestones/Chroma2-Kaleidoscope/blob/7b2212134813e2a63dc8ccde06065992c9d706de/README.md), [Zeta pinned card](https://huggingface.co/lodestones/Zeta-Chroma/blob/38ddc5f3e26fbbcb0ee4ff374b3f5cc384376d4b/README.md)

HD's current source revision and the GGUF repository revision differ. That is normal for separate repositories, but repository names and timestamps alone do not establish the quantization's exact parent weights. Record both revisions and qualify the actual quantized file rather than claim byte-equivalence with today's source checkout.

### Qwen-Image 2.1 Uncensored

The publisher describes a targeted LoRA merged into the generator and identifies 128 attention projections as the modified layers. A 33,586,704-byte adapter is published. This is a concrete, inspectable claim, but this investigation did not download and reconstruct that merge or independently benchmark its effect. Criticism in the discussion is also not proof of fraud: comparing only unchanged BF16 tensors cannot establish that the stated attention projections are unchanged. [Author discussion](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/discussions/20)

There is a meaningful naming distinction: select an explicit `-UC-` artifact when evaluating the claimed modified model; the current README also links ordinary base artifacts on a separate branch. The UC Q4_K_M generator is 4,604,558,112 bytes, but the suggested INT8 encoder is 9,350,798,360 bytes and the VAE is another 675,509,688 bytes. The claim that CPU encoding has virtually no speed cost is the author's assertion, not a measurement for our machine. [Pinned inventory/card](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/blob/6b34e59458d3eb7ba6a6f86a116aed5253dc02c3/README.md)

The upstream Qwen Research License limits the model materials to research/evaluation and requires a separate commercial license. This matters for a product candidate regardless of the encoder's license. [Upstream terms, sections 1.i and 2](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE)

### Noct Q and Kroma: additional candidates, not drop-in upgrades

Noct Q V4 advertises broader adult-content capability from modified Qwen generator weights. Its current card advertises a 7.3 GB INT8 file for 8–12 GB cards, but it still requires the large Qwen encoder and VAE. The author's claimed improvement over V3 is not an independent result. It inherits the Qwen Research License. [Pinned Noct Q card](https://huggingface.co/Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1/blob/a81b9af51120a78e285e57906f2250a2a02080e9/README.md)

Kroma exposed a source-freshness problem. Exa returned a v0.1 LoRA card describing 1.88 GB and MIT. Direct retrieval at current revision `b921d45c0f2c33ab753a32e32a5fe34eb790344c` instead describes v0.2 full checkpoints, while the repository also contains v0.3 weights. Turbo files are 25,640,191,096 bytes before the separate encoder and VAE. The current card's front matter names Krea's community license while its body retains MIT language and explicitly preserves upstream terms. Do not treat that as a blanket MIT license for the entire pipeline or a small self-contained model. [Current pinned Kroma card](https://huggingface.co/lodestones/Kroma/blob/b921d45c0f2c33ab753a32e32a5fe34eb790344c/README.md), [official Krea repository](https://github.com/krea-ai/krea-2)

### Encoder-only changes: insufficient evidence for the goal

The author of the Z-Image abliterated encoder explicitly does not claim better quality or consistency and acknowledges artifacts. A later explanation emphasizes altered interpretation. These statements are narrower than the label's implied guarantee. [Discussion 3](https://huggingface.co/BennyDaBall/Qwen3-4b-Z-Image-Turbo-AbliteratedV1/discussions/3), [discussion 4](https://huggingface.co/BennyDaBall/Qwen3-4b-Z-Image-Turbo-AbliteratedV1/discussions/4)

A firsthand Z-Image/Heretic study compares matched encoder variants and reports no measured uncensoring benefit on its limited test set. The author discloses having published the encoder under test; this is not an independent peer-reviewed validation. Its ordinary prompt suite is small, the sensitive subsets are smaller, and results do not generalize to every encoder, quantizer or image model. Its practical contribution is experimental design: separate quantization changes from encoder modifications and judge images, not only a chat refusal metric. [Study and methods](https://abliterlitics.dev/posts/z-image-text-encoder/)

## Target-host feasibility

An earlier host snapshot showed RTX 2070-class hardware, approximately 8 GB nominal VRAM, 10.4 GiB available system RAM, fully used 8 GiB swap, and mismatched loaded/installed NVIDIA versions. **The subsequent NVIDIA Skills preflight supersedes that readiness snapshot:** `nvidia-smi` now works, loaded/installed versions match at 595.91.07, approximately 17.2 GiB system RAM is available, and swap is almost unused. CUDA initialization also succeeds. This is host readiness evidence, not an image inference benchmark. See the [dated preflight](nvidia-chroma-preflight-2026-10-02.md).

| Chroma component | Stored bytes | Provenance |
|---|---:|---|
| Chroma1-HD Q4_K_M | 5,566,533,792 | `silveroxides/Chroma1-HD-GGUF@d3b77bf4eb5b84c0b50e9a1e83f6e8074ed85f82` |
| T5 XXL Q5_K_M | 3,386,856,640 | `city96/t5-v1_1-xxl-encoder-gguf@005a6ea51a7d0b84d677b3e633bb52a8c85a83d9` |
| Original-format FLUX VAE `ae.safetensors` | 335,304,388 | `lodestones/Chroma@2f3b2730d7b5edbd02cdff12f72610af5787300b` |
| Alternative Diffusers VAE | 167,666,902 | `lodestones/Chroma1-HD@0e0c60ece1e82b17cb7f77342d765ba5024c40c0` |

Choose one VAE representation appropriate to the runtime; they are not interchangeable filenames. The first three entries total about 9.29 GB of stored weights. This is neither peak VRAM nor peak host RAM. Activations, temporary dequantization, loading copies, conditioning and VAE decoding need additional memory. Use staged component residency and measure the entire job, including CPU encoding. [GGUF files](https://huggingface.co/silveroxides/Chroma1-HD-GGUF/tree/d3b77bf4eb5b84c0b50e9a1e83f6e8074ed85f82), [T5 files](https://huggingface.co/city96/t5-v1_1-xxl-encoder-gguf/tree/005a6ea51a7d0b84d677b3e633bb52a8c85a83d9), [VAE source](https://huggingface.co/lodestones/Chroma/blob/2f3b2730d7b5edbd02cdff12f72610af5787300b/ae.safetensors)

## Runtime recommendation: a bounded C++ qualification first

Use **stable-diffusion.cpp** as the first Chroma GGUF runtime to qualify, compiled for SM75 and launched as a bounded subprocess. This is an integration recommendation, not a verified speed ranking. Pin commit `3f8527a46c54ecf4cb4ed6003da8e8982283c73c` and its bundled GGML revision, rather than a floating branch. Its current backend documentation separates compute placement, weight storage and managed VRAM budget. The latter is not a physical cap on driver allocations or unrelated processes. Start with text encoding on CPU and explicit component placement; archive the actual binary's help and device listing before constructing job arguments. [Backend placement](https://github.com/leejet/stable-diffusion.cpp/blob/3f8527a46c54ecf4cb4ed6003da8e8982283c73c/docs/backend.md)

The old documented 4/6 GB Chroma example uses v40 and does not qualify HD or Flash. NVIDIA's SM75 capabilities provide native FP16 but not native BF16/FP8/NVFP4 Tensor Core execution. A GGUF Q4 weight format is not NVFP4 hardware arithmetic. The runtime's SageAttention integration requires SM80 or newer, so it is not the starting path for RTX 2070. [Chroma example](https://github.com/leejet/stable-diffusion.cpp/blob/3f8527a46c54ecf4cb4ed6003da8e8982283c73c/docs/chroma.md), [NVIDIA capabilities](https://docs.nvidia.com/cuda/archive/13.2.2/cuda-programming-guide/05-appendices/compute-capabilities.html), [SageAttention requirements](https://github.com/leejet/stable-diffusion.cpp/blob/3f8527a46c54ecf4cb4ed6003da8e8982283c73c/docs/sage_attention.md)

| Runtime | Why consider it | Remaining qualification |
|---|---|---|
| stable-diffusion.cpp | Native GGUF, process isolation, explicit component/storage placement | Exact HD quantization, SM75 build, numerical output and transfers |
| ComfyUI + GGUF plugin | Visual workflows, broad model ecosystem | Pinned core/plugin compatibility, RAM residency and cancellation |
| Diffusers Chroma | Official Python pipeline, familiar integration | Separate GGUF transformer loading, compute dtype, full pipeline memory and offload |

A C++ issue reports a twofold Z-Image regression on an RTX 2080 Ti after a GGML change; that is a related SM75 warning, not a reproduction on Ubuntu/RTX2070 or a Chroma result. ComfyUI issues similarly report large-encoder RAM residency and an 8 GB GGUF initialization problem. Use these to design tests, not to assert that every installation has these defects. [C++ issue 1818](https://github.com/leejet/stable-diffusion.cpp/issues/1818), [ComfyUI 14433](https://github.com/Comfy-Org/ComfyUI/issues/14433), [ComfyUI 14573](https://github.com/Comfy-Org/ComfyUI/issues/14573)

Diffusers' GGUF support separates compact stored weights from `compute_dtype` during execution. Its CPU offload choices trade GPU memory for transfers; they do not erase host memory demand. Use one strategy per measured configuration. [GGUF documentation](https://huggingface.co/docs/diffusers/quantization/gguf), [memory strategies](https://huggingface.co/docs/diffusers/optimization/memory)

## What research can establish

Consensus records were fetched before citation. Sider Scholar records were followed to primary texts; conference status was checked separately. None of the following studies directly certifies the exact local Chroma or Qwen UC package.

| Primary study | Status | Applicable lesson |
|---|---|---|
| [GenEval 2](https://arxiv.org/html/2512.16853v1) | Preprint | Decompose prompts into observable requirements; automatic evaluator scores can drift from human judgment. |
| [Gecko](https://proceedings.iclr.cc/paper_files/paper/2025/hash/0114e631f415a438d8fbb7c9a99c10c8-Abstract-Conference.html) | ICLR 2025 | Evaluate distinct skills and retain human assessment; rankings depend on the prompt set and evaluation scheme. |
| [OVERT](https://proceedings.nips.cc/paper_files/paper/2025/hash/aec2c695e9efff95bd43650699c6af36-Abstract-Datasets_and_Benchmarks_Track.html) | NeurIPS 2025 Datasets & Benchmarks | Measure over-refusal on benign requests; API, UI and external filters form part of the tested system. |
| [SVDQuant](https://proceedings.iclr.cc/paper_files/paper/2025/hash/f34f0630c33be15b8c89426bb8056798-Abstract-Conference.html) | ICLR 2025 | Four-bit quality and speed depend on quantization method and execution kernels, not just the nominal bit count. |

OVERT specifically distinguishes API/UI configurations and includes an external filter in its SD3.5 evaluation. Its rates cannot be attributed to an arbitrary raw checkpoint. SVDQuant's results are for its method and evaluated hardware; they are not benchmarks of GGUF Q4 on this card. Gecko and GenEval 2 address fidelity assessment, not a universal absence of restrictions.

## Smallest useful qualification and subsequent pilot

The subsequent preflight clears the earlier telemetry warning. Prepare a complete pinned model inventory and compatible SM75 runtime outside production. Then run batch 1 at 512 square with a fixed seed, the author's recipe, explicit compute placement and a measured memory reserve. Leave automatic weight placement enabled initially; setting `--backend` preserves it, whereas an explicit `--params-backend` disables it. This first small image is a runtime smoke test, not a quality benchmark at the model's preferred resolution. Capture load, text encoding, denoising and VAE time separately; inspect the image for numerical degeneration. Exercise cancellation followed by a successful new job and confirm memory release before widening resolution.

If that succeeds, compare 40 ordinary prompts with 3 seeds each per configuration: counting/spatial composition, French text with accents, product/portrait/landscape, illustration/layout, and benign ambiguous vocabulary. Use a written requirement checklist and blind ratings, ideally from two reviewers. This 120-image pilot is our proposed engineering study, not a complete reproduction of the papers and not proof about all content categories.

Record these outcomes separately:

- Load failure, OOM, timeout and cancellation.
- Explicit refusal or a filter decision supported by a trace.
- Black/blank output of unknown cause; a black image alone is not evidence of filtering.
- A generated image that misses or changes requested details.
- Successful image with all observable requirements satisfied.

Store original prompt and actual encoder input, component revisions and hashes, runtime/compiler/driver identity, scheduler, precision, quantizer, placement, steps, guidance, resolution, seed and image hash. Compare aesthetic quality separately from requirement adherence. Report failures, median/p95 latency, peak RSS/VRAM and swap activity; do not discard failed runs to improve averages. Multiple seeds of one prompt are correlated, and the same seed across different architectures is not an identical latent experiment. A quantization comparison needs the same parent checkpoint and an available precision reference; if the reference cannot run, state that limitation.

## Implications for Swarmer

The existing local image adapter is fixed to SDXL-Lightning: separate base and UNet, Euler trailing, four steps and guidance zero. Its profile schema does not accept a Chroma backend or `.gguf` files. Moreover, the request contract caps sampling at 30 steps, below the HD author's 40-step example. A renderer-only replacement would be incomplete. [Renderer](../../workers/media-worker/render.py), [worker contract](../../workers/media-worker/media_contract.py), [server contract](../../server/app/services/media_contracts.py)

A subsequent implementation should add a closed, versioned Chroma profile and adapter; explicitly represent its recipe and component hashes; then update bounded request validation and add regression tests. Keep jobs unable to select arbitrary paths, executables or download URLs. Preserve the existing GPU admission, cancellation, lease, authenticated media storage and ownership checks. Those controls address resource use and result integrity independently of model content behavior.

No production dependency changes, GPU job, download, deployment, commit or phone test occurred in this research pass. Existing source work and the user's two original research artifacts remain separate.

## Research provenance and reproducibility

The requested Exa, Hugging Face, GitHub, Consensus and Sider Scholar tools were exercised. Work was split into candidate provenance, runtime feasibility and scientific evaluation, with a separate integration synthesis. Exa issued 13 searches requesting 83 result slots: 34 for alternatives/provenance, 20 for the Chroma family, and 29 for runtimes. These are search slots with duplicates, **not 83 independent sources read in full**. Primary pages were fetched after discovery; duplicates, marketplace summaries and stale mirrors were not treated as independent support.

The Chroma investigation read eight full documents and eight bounded discussion threads. Runtime review used four Exa document extracts, 12 GitHub source/documentation files, three issue bodies and four commit records. The evidence review fetched three Consensus records and a Sider Scholar record before consulting the four primary papers and publication records. Counts from different workstreams overlap and should not be summed into a unique-source claim.

The HF model-search tool was advertised but failed with `Tool model_search not found`; repository-detail access and public Hub metadata worked. One Exa discussion fetch timed out and was read through another web tool. The Krea LICENSE fetch returned 401; no authentication bypass was attempted and no complete interpretation of its unavailable text is asserted. The Kroma discrepancy was resolved by fresh revision-pinned raw content, not by choosing whichever search excerpt supported the recommendation.

Two C++ documentation revisions appeared during parallel research (`3f8527a46c54ecf4cb4ed6003da8e8982283c73c` and `bb84971129d2a094ab8051c6feed5406d3b4409d`). No executable was built from either. The qualification must use one exact source/dependency set and its own help output; commands from different revisions must not be mixed.

The companion [evidence inventory](uncensored-image-model-deep-dive-2026-10-02.json) records candidate revisions and selected Hub-reported weight hashes. Those hashes identify advertised artifacts; they are not locally verified downloads. An absent controlled comparison remains absent evidence, regardless of downloads, likes or confident author language.
