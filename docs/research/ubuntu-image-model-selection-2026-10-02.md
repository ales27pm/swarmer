# Ubuntu image-model selection — 2026-10-02

Research recommendation: start with **RealVisXL 5 Lightning for photography**, **Animagine XL 4.0 Opt for illustration**, or **Chroma1-HD when an intentionally unaligned base matters most**. FLUX.2 Klein 4B and Z-Image-Turbo remain useful modern candidates, but their abliterated text encoders should be optional controlled experiments, not assumed upgrades.

This assessment combines live read-only Ubuntu inspection, Hugging Face metadata, creator cards/discussions, and runtime source. No model weights were downloaded, no inference was performed, and no service, driver, or existing source file was changed. Rankings describe expected fit and evidence quality, not measured image-quality superiority.

Selected revisions, exact filenames, sizes and available Hub-reported LFS SHA-256 values are saved in [the metadata inventory](ubuntu-image-model-candidates-2026-10-02.json). The gated Cordux file's hash is redacted in public metadata and is recorded as unavailable. Available hashes have not been checked against locally downloaded weights. The inventory is not a complete installer or runtime dependency lock.

## Verified machine and current constraints

The host reports Ubuntu 26.04.1 LTS, Intel i7-11700, NVIDIA RTX 2070, approximately 30 GiB usable RAM, 10 GiB currently available RAM, full 8 GiB swap, and 197 GiB available root storage. Its default Python is 3.14.4. The RTX 2070's standard VRAM specification is 8 GB; a live capacity/free-memory query could not complete. [NVIDIA specification](https://www.nvidia.com/content/nvidiaGDC/gb/en_GB/geforce/graphics-cards/rtx-2070.html)

The loaded NVIDIA module is 595.84; the installed module and user-space library are 595.91.07. `nvidia-smi` fails with a driver/library mismatch and Ubuntu reports a required restart. A planned restart is the likely first maintenance step because the on-disk versions agree, but successful recovery must be verified afterward. Research did not authorize interrupting running services, so no restart or process termination occurred.

Offloading moves model memory into system RAM. It does not eliminate memory use. With current memory pressure, successful loading cannot be inferred from 32 GB installed RAM or the sum of weight-file sizes.

## Revised shortlist

Sizes below are decimal GB of stored files, not peak GPU memory. SDXL single checkpoints include their main model components; GGUF generators require separate encoders and VAEs.

| Candidate | Documented property | Suggested evaluation configuration | Assessment |
| --- | --- | --- | --- |
| [RealVisXL 5 Lightning](https://huggingface.co/SG161222/RealVisXL_V5.0_Lightning) | Creator describes photorealistic SFW/NSFW capability; not an abliteration claim; OpenRAIL++ | `RealVisXL_V5.0_Lightning_fp16.safetensors`, 6.938 GB; author recommends 5 steps, DPM++ SDE/Karras, CFG 1–2 | First photography baseline; low step count favors iteration |
| [Animagine XL 4.0 Opt](https://huggingface.co/cagliostrolab/animagine-xl-4.0) | Creator documents content-rating tags; not abliterated; OpenRAIL++ | `animagine-xl-4.0-opt.safetensors`, 6.938 GB; Euler a, 28 steps, CFG 5; author targets roughly 1024² | First anime/illustration baseline; use tagged prompts |
| [Chroma1-HD](https://huggingface.co/lodestones/Chroma1-HD) | Creator explicitly documents absence of specific safety alignment; Apache 2.0 | [Q4_K_M generator](https://huggingface.co/silveroxides/Chroma1-HD-GGUF), 5.567 GB, plus T5-XXL and FLUX.1 VAE; author example uses 40 steps, CFG 3 | Stronger documented unaligned-design candidate; heavier and more work per image |
| [FLUX.2 Klein 4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) | Compact generation/editing model; stock model is not established as uncensored; Apache 2.0 | [Q4_K_M generator](https://huggingface.co/unsloth/FLUX.2-klein-4B-GGUF), 2.604 GB; correct Qwen3-4B encoder and FLUX.2 VAE; distilled variant uses 4 steps, CFG 1 | Best modern candidate for generator memory margin; test stock conditioning first |
| [Z-Image-Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) | Efficient image generator; encoder replacement alone does not establish broader image compliance; Apache 2.0 | [Q4_K_M generator](https://huggingface.co/unsloth/Z-Image-Turbo-GGUF), 5.018 GB; Qwen3-4B encoder and its own `ae.safetensors`; use runtime's Turbo recipe | Worth comparing for photography; tighter GPU margin than Klein |
| [Qwen-Image 2.1 Uncensored](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF) | Author describes merged generator LoRA fine-tuning; broad benefit remains unverified; Qwen Research license | UC Q4_K_M generator, 4.605 GB; substantial encoder, projector/editing requirements and model-specific VAE | Later experiment, not first installation on this host |

For Chroma, the [T5 GGUF publisher](https://huggingface.co/city96/t5-v1_1-xxl-encoder-gguf) offers Q5_K_M at 3.387 GB and Q8_0 at 5.062 GB. Encoder placement and precision must be compared separately from generator quantization. Do not interpret a 5.567 GB Chroma generator as a complete 5.567 GB pipeline.

## Why the encoder recommendation changed

Three different properties are routinely called uncensored: no external output checker, a generator trained to cover broader subject matter, and a text encoder whose chatbot refusal behavior has been modified. These are not interchangeable evidence.

The official [FLUX Klein pipeline](https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/flux2/pipeline_flux2_klein.py) conditions images on hidden layers 9, 18 and 27. The official [Z-Image pipeline](https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/z_image/pipeline_z_image.py) uses the penultimate hidden state. Neither step generates a chat answer. A refusal-rate or next-token KL measurement therefore does not directly measure the image generator's compliance or preserved visual conditioning.

[Cordux's encoder](https://huggingface.co/Cordux/flux2-klein-4B-uncensored-text-encoder) derives from a generic abliterated Qwen3-4B checkpoint, lacks a controlled image comparison, and explicitly says it cannot add missing visual knowledge. Its Q4_0 file is 2.370 GB; access to the original repository is gated. Matching architecture makes it a plausible experiment, not a demonstrated improvement.

[BennyDaBall's own discussion](https://huggingface.co/BennyDaBall/Qwen3-4b-Z-Image-Turbo-AbliteratedV1/discussions/3) says the original Z-Image is already permissive, does not claim better quality/consistency, and acknowledges artifacts. [A further explanation](https://huggingface.co/BennyDaBall/Qwen3-4b-Z-Image-Turbo-AbliteratedV1/discussions/4) frames the change as prompt interpretation. This supports testing it as a variation rather than making it the default.

A [firsthand controlled Z-Image study](https://abliterlitics.dev/posts/z-image-text-encoder/) found no measured uncensoring improvement for its own Heretic encoder. It supplies paired images and quantitative analysis, but uses different weights from Benny, has a small sensitive-content subset, and had a pending complete-artifact release. Its result is cautionary evidence, not proof that every encoder edit is ineffective. Its problematic INT4 conversion also cannot be generalized to all GGUF Q4 variants.

The official Comfy-Org packages contain byte-identical stock Qwen3-4B encoders for FLUX Klein and Z-Image: 8,044,982,048 bytes, SHA-256 `6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a`. Their VAEs differ. Exact package revisions are in the inventory; do not swap the FLUX.2 and Z-Image VAEs.

## Qwen: more concrete provenance, more runtime complexity

In [discussion 20](https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/discussions/20), the author explains that a dataset was used to train a LoRA, then merge it into the image generator. A 33,586,704-byte adapter is now published. That establishes a disclosed fine-tuning approach and an available artifact; it does not independently establish the claimed behavioral improvement, merge correctness, or unchanged quality. The text encoder can remain stock according to the author.

The earlier 9.35 GB INT8 encoder is not the only route. [Pottokao's alternative](https://huggingface.co/pottokao/Qwen-Image-2.1-Text-Encoder-Heretic-GGUF) provides a 5.028 GB Q4 encoder plus a 1.159 GB vision projector, with a custom ComfyUI loader patch. This lowers stored weight size while adding another experimental encoder modification and loader dependency. It still needs the 4.605 GB generator and a 676 MB VAE. Its Apache-licensed encoder does not change the image generator's [research/evaluation-only license](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE).

The [sd.cpp Qwen guide](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/qwen_image_2.1.md) also documents prefix-cache overhead: its 4,096-token FP16 example uses 2 GiB per condition, with separate positive/negative caches. This is an example, not a typical short-prompt measurement. Cache policy, resolution, attention buffers and VAE decode all affect the actual peak.

## Runtime choice and Swarmer implications

For interactive comparisons, evaluate **ComfyUI v0.38.0** with the GGUF loader in a separate environment. That release includes Qwen 2.1 FP16 and cache fixes. Its [pinned installation instructions](https://github.com/Comfy-Org/ComfyUI/blob/v0.38.0/README.md#manual-install-windows-linux) favor Python 3.13 and require current CUDA 13 PyTorch for NVIDIA 20-series and newer; Python 3.14 can expose custom-node incompatibilities. This is upstream guidance, not an environment qualified on this host. [Release fixes](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.38.0)

For a future headless worker, evaluate **stable-diffusion.cpp [master-929-3f8527a](https://github.com/leejet/stable-diffusion.cpp/releases/tag/master-929-3f8527a)**. It supports the relevant model families and GGUF without a Python inference stack. Family support does not validate every third-party encoder or quantization. [Memory controls](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/performance.md)

RTX 2070 is Turing/SM75: begin with FP16 compute and compatible GGUF kernels. It lacks native BF16/FP8/FP4 tensor acceleration; GGUF Q4 is not NVFP4. Avoid copying newer-GPU presets. In particular, sd.cpp's native SageAttention requires SM80 or newer. [NVIDIA capability table](https://docs.nvidia.com/cuda/archive/13.2.0/cuda-programming-guide/05-appendices/compute-capabilities.html), [sd.cpp requirements](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/sage_attention.md)

The current local [Swarmer worker](../../workers/media-worker/README.md) supports only a fixed four-step SDXL-Lightning profile, FP16, Euler trailing, guidance zero, and 512/768 dimensions. It is not CUDA-qualified. RealVis's author settings differ, so even that candidate needs a separate profile/adapter adjustment. FLUX, Z-Image, Chroma and Qwen need additional adapters. The existing isolated dependency candidate must not be mistaken for the modern ComfyUI stack. No integration was attempted.

## Candidates deliberately not promoted

[Chroma2-Kaleidoscope](https://huggingface.co/lodestones/Chroma2-Kaleidoscope) and [Zeta-Chroma](https://huggingface.co/lodestones/Zeta-Chroma) have promising newer backbones but very thin WIP cards. [Chroma1-Radiance](https://huggingface.co/lodestones/Chroma1-Radiance) documents ongoing training/artifact limitations. [Illustrious-Lumina](https://huggingface.co/OnomaAIResearch/Illustrious-Lumina-v0.03) is explicitly a proof of concept. [NoobAI XL 1.1](https://huggingface.co/Laxhar/noobai-XL-1.1) is capable, but its author restricts commercial use including generated products; Animagine is the more practical initial illustration choice. These are specific maturity/licensing judgments, not measured quality rankings.

## Qualification plan, not executed

After the driver is healthy and sufficient memory is deliberately made available, test one model at a time, batch one, without upscaling, ControlNet or unrelated LoRAs. Record runtime revision, model hashes, GPU residency, available RAM and swap state.

Use six benign prompts covering portrait/hands, product photography, an interior, counted objects/spatial relations, landscape and a bilingual sign. Use three seeds per prompt. Begin with a 768² smoke test, then compare at 1024² where successful. Keep each model's recommended sampler/step recipe; equal step counts are not a fair comparison between distilled and ordinary models.

Measure cold loading, prompt encoding, denoising, VAE decoding, total elapsed time, peak VRAM/RSS, swap activity and failures. Change seeds during warm runs so graph caching cannot masquerade as inference. Compare stock/modified encoders with everything else fixed and assess adherence, anatomy, lettering and artifacts blind. Quantization experiments should be separate from abliteration experiments. Broad uncensored behavior requires its own lawful, task-appropriate image tests; the benign suite alone cannot establish it.

No defensible exact seconds-per-image estimate for this host was found. No local image quality, throughput, sustained-load, cancellation, restart-recovery, production or phone result is claimed.
