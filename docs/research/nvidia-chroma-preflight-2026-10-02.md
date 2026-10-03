# NVIDIA Skills: RTX 2070 / Chroma preflight

Observed on 2026-10-02 in Montreal, including remote probe `2026-10-03T01:53:43.814601+00:00`. Read-only Ubuntu inspection. No image inference, model download, driver change, service restart or deployment occurred.

**Subsequent authorized fix and qualification:** the PATH issue and missing renderer have been addressed. CMake is accessible in fresh SSH sessions, and a native SM75 CUDA `sd-cli` is installed with two numerical GPU checks passing. See the [installation receipt](../evidence/chroma-runtime-2026-10-02.md). Full Chroma inference subsequently produced two complete 512 × 512 images, with cancellation and recovery verified; see the [image qualification](../evidence/chroma-inference-2026-10-02.md). The table and candidate discussion below preserve the earlier preflight observations.

## Skill discovery

The installed `nvidia-skill-finder` was consulted. `npx --yes skills add nvidia/skills --list` initially failed because the user's npm cache contains root-owned entries. Repeating it with a fresh temporary npm cache succeeded and listed 398 skills. No new skill was installed.

The current [official catalog](https://github.com/NVIDIA/skills) and two candidates were inspected:

- [`tao-setup-nvidia-gpu-host`](https://github.com/NVIDIA/skills/blob/main/skills/tao-setup-nvidia-gpu-host/SKILL.md) checks drivers, CUDA tooling and GPU containers for TAO. Its default version requirements are TAO-specific, not Chroma requirements. No TAO installer was run.
- [`dynamo-troubleshoot`](https://github.com/NVIDIA/skills/blob/main/skills/dynamo-troubleshoot/SKILL.md) targets Dynamo/Kubernetes deployments; it is not a match for the proposed single-host C++ image renderer.

No directly applicable Chroma/RTX 2070 execution recipe was identified in this catalog review. Runtime-specific documentation remains necessary.

## Live Ubuntu results

| Probe | Result |
|---|---|
| `nvidia-smi` | Success; RTX 2070, compute capability 7.5 |
| VRAM | 8,192 MiB total; 361–362 MiB used across the two observations |
| GPU temperature | 39 C at the second observation |
| Loaded and installed driver | Both 595.91.07 |
| Reboot marker | Absent |
| Available system RAM | 18,060,752 KiB, approximately 17.2 GiB |
| Swap | 344 KiB used out of 8,388,604 KiB |
| `cuInit(0)` | Status 0 |
| `cuDriverGetVersion` | Status 0, value 13020; driver API capability, not proof of an installed CUDA compiler |
| `nvidia-ctk --version` | 1.20.0 |
| `cmake` | Not found in the SSH session PATH |
| CUDA compiler search | No `/usr/local/cuda*/bin/nvcc` found; other locations were not exhaustively searched |
| Image renderer | `sd-cli`, `sd`, `stable-diffusion`, `stable-diffusion-cli` not found in that PATH |

The earlier driver/library mismatch and full swap are historical observations. This pass did not repair them and does not attribute their resolution to any action here. GPU telemetry and CUDA initialization do not yet prove a model can execute.

## Candidate configuration, not an executed test

Use an SM75-compatible CUDA build with FP16-capable kernels. Turing supports FP16 and integer Tensor Core operations, but not native BF16, FP8 or NVFP4 acceleration. GGUF Q4 storage does not imply NVFP4 arithmetic. [NVIDIA Turing guide](https://docs.nvidia.com/cuda/turing-tuning-guide/index.html), [CUDA capabilities](https://docs.nvidia.com/cuda/archive/13.2.2/cuda-programming-guide/05-appendices/compute-capabilities.html).

At the selected stable-diffusion.cpp revision, SageAttention requires SM80 or newer; exclude `--sage-attn` on this GPU. [Pinned requirements](https://github.com/leejet/stable-diffusion.cpp/blob/3f8527a46c54ecf4cb4ed6003da8e8982283c73c/docs/sage_attention.md).

First qualify Chroma1-HD Q4 with batch 1 at 512 x 512, text encoding and VAE on CPU, diffusion on CUDA. Candidate placement flags, to confirm against the actual built binary:

```text
--backend diffusion=cuda0,te=cpu,vae=cpu
--max-vram cuda0=6.5
```

The 6.5 GiB managed budget is an experimental starting point. It excludes some driver/external allocations. Leaving `--params-backend` unset preserves automatic weight placement; record the resulting placement. Explicit weight placement disables this mechanism. [Pinned backend documentation](https://github.com/leejet/stable-diffusion.cpp/blob/3f8527a46c54ecf4cb4ed6003da8e8982283c73c/docs/backend.md).

Before claiming compatibility, obtain the complete pinned model components, prepare a compatible executable, and measure a real generation plus cancellation and memory release. Record load, encoding, denoising and decoding separately, together with peak host/GPU memory and swap. No latency or image-quality result is available yet.
