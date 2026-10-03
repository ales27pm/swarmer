# Chroma CUDA runtime installation on Ubuntu

Completed 2026-10-02 Montreal time. Scope: make CMake and a CUDA-enabled `sd-cli` available to the Ubuntu account, and validate the executable and two small GPU computations. No Chroma weights were downloaded and no image was generated. The Swarmer media worker was not deployed or changed by this operation.

## Installed and repaired

- CMake 4.4.0 already existed in `/home/ales27pm/.local/bin`. Non-interactive SSH missed it because the PATH setup appeared after Bash's interactive-shell early return. An idempotent PATH block was inserted before that guard, with a private backup of the original file. The existing CMake executable was preserved.
- A private Python 3.12 tool environment contains `cmake==3.31.6` and `ninja==1.11.1.4`. Existing CMake commands were not overwritten; only the missing Ninja command was linked from this environment. The private CMake is an unused fallback, not the compiler-build CMake or the account's default CMake.
- `stable-diffusion.cpp` commit `3f8527a46c54ecf4cb4ed6003da8e8982283c73c`, with GGML commit `89c4413f5da6fb20cc796f16033d37f129be81fd`, was compiled for CUDA architecture **75**, matching the RTX 2070.
- Installed native executable: `/home/ales27pm/.local/share/swarmer/chroma-runtime/3f8527a-sm75/bin/sd-cli`. User command: `/home/ales27pm/.local/bin/sd-cli`.

The build reused the existing image `sha256:c55ec3428181057adc0a92a8cf844cb7d3aa029dc2c154bc8baca5fc6c7ede5f`, containing CUDA 12.8.93, GCC 11.4 and CMake 3.22.1. The build container had no network, a read-only source mount, the invoking user's UID/GID, dropped capabilities, and resource limits. Two compilation jobs were used. Driver and production services were not replaced or restarted.

Key build options: `SD_CUDA=ON`, `CMAKE_CUDA_ARCHITECTURES=75`, `GGML_STATIC=ON`, `GGML_CUDA_NCCL=OFF`, `SD_USE_UPSTREAM_GGML=OFF`, shared-library options off, WebP/WebM off. The initial build automatically found NCCL; it was deliberately stopped and resumed with NCCL disabled, retaining completed objects. This was not an OOM. PNG support remains available.

## Verified results

| Check | Result |
|---|---|
| Build | Exit 0, `sd-cli` linked |
| Host `ldd` | All dependencies resolved; no external cuBLAS, CUDA runtime or NCCL library required |
| Native `--version` | Exit 0, commit `3f8527a`; version label `unknown` because this is a shallow commit checkout without its release tag |
| Native `--help` | Exit 0 |
| Native `--list-devices` | CUDA0 = NVIDIA GeForce RTX 2070, compute capability 7.5; CPU also listed |
| CUDA FP16 matrix multiplication | Compared against CPU reference: OK |
| CUDA Q4_K matrix multiplication | Compared against CPU reference: OK |
| GPU test summary | **2/2 tests passed**, exit 0; both expected cases actually executed |
| Fresh SSH session | Finds `cmake`, `ninja`, and `sd-cli` without a manual PATH override; all version commands exit 0 |
| Backend checkpoint during build | `/health` returned HTTP 200, status `ok`, version `0.14.2` |

The CPU backend's top-level test pass counter includes a skipped backend; the meaningful evidence is the two CUDA operation lines and their `2/2 tests passed` summary. Test dimensions were `m=1,n=64,k=256`, with FP32 activations and either FP16 or Q4_K weights. These tests do not establish full Chroma inference, peak model memory, image quality, or specific Tensor Core usage.

Installed binary: 893,247,080 bytes; SHA-256:

```text
3e3f105d52840e06dd133c862abd5c6895c2d974abf92e9487b963b5decfa6cf
```

The [machine-readable receipt](chroma-runtime-2026-10-02.json) includes the exact Docker build command, source identities, test output and fresh SSH checks. Detailed build/test logs and the shell backup remain under `/home/ales27pm/swarmer-media-qualification/20261002/chroma-3f8527a-sm75` on Ubuntu.

At the end of this installation, complete model inference, cancellation and memory-release checks remained outstanding. The subsequent [Chroma image qualification](chroma-inference-2026-10-02.md) completed those checks with two successful images and a deliberate cancellation between them. This installation does not expose a new application API or change the existing SDXL media profile.
