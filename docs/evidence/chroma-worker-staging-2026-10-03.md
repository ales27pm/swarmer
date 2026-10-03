# Chroma worker deployment preparation

Prepared on 2026-10-03 for the user-authorized Swarmer Chroma deployment. This
receipt covers staging and isolation checks; registration, service activation,
and a real leased image job are performed separately by the deployment owner.

## Immutable artifacts

- Release: `media-chroma-ee3d039ac6215a5cd2d7`.
- Archive: `/tmp/media-chroma-ee3d039ac6215a5cd2d7.tar.gz` on Mac and Ubuntu.
- Archive SHA-256: `b0d39b0a6288ef8a787b8d7a6e14d0ecc0ff9e17bf6b25e11c698d7588e7f1e1`.
- Ubuntu release: `/home/ales27pm/.local/share/swarmer-chroma-worker/releases/media-chroma-ee3d039ac6215a5cd2d7`.
- `release.json` pins 12 scoped files, including the sibling file-worker protocol.
- Python environment: `/home/ales27pm/.local/share/swarmer-chroma-worker/venv`,
  Python 3.12.13 with `Pillow==12.3.0`; no Torch/Diffusers installation.
- Model profile: `/home/ales27pm/.local/share/swarmer-chroma-worker/models/chroma1-hd-q4-sm75-v1/profile.json`.
  The three existing weights and native executable were copied to that new
  directory, verified against all four manifest size/SHA-256 pins, and made
  read-only (runtime executable). Original model files were not modified.
- Model copy receipt: `/home/ales27pm/.local/share/swarmer-chroma-worker/model-stage-receipt.json`.

## Deployment boundary corrections

User-systemd `IPAddressDeny=any` with `IPAddressAllow=localhost` was tested in a
disposable unit. Both loopback and external connections succeeded, so those
properties are not used as evidence of network isolation on this host.

The worker now optionally wraps its renderer in Bubblewrap with an offline
network namespace, read-only interpreter/libraries/code/model inputs, GPU device
mounts and writable per-attempt scratch. Deployed configuration enables this
mode; unavailable Bubblewrap fails the job instead of falling back. The trusted
parent retains its fixed loopback control-plane/Ollama transport. The production
venv uses the system Python; the sandbox also handles uv's version-alias symlink.

The worker acquires the same exclusive POSIX flock as Chroma Studio before
claiming work and holds it through the attempt. The configured path is
`/home/ales27pm/.local/state/swarmer-gpu/image-generation.lock`. It is not unlinked
or replaced. A fixed read-only Docker inventory query additionally rejects
orphan Studio containers after a Studio crash. Unknown inventory fails closed.

## Checks completed

- 89 local worker tests passed, including cross-process lock contention/release,
  no-claim while busy, orphan-container fencing, and existing cancellation/PNG
  contracts. Ruff passed.
- `ldd` on the pinned native executable resolved all host libraries. No missing
  external CUDA runtime, cuBLAS or NCCL dependency was found.
- Real Ubuntu Bubblewrap probe succeeded with both system-Python and uv-Python
  virtual environments: external connection returned errno 101; host API
  connection returned errno 111; private configuration was invisible; code write
  was rejected; scratch write and Pillow import succeeded.
- Staged worker import succeeded and `verify_profile` rechecked all four files.
- `swarmer-chroma-worker.service` was installed and the user manager reloaded;
  the service remained **inactive** at this handoff. No agent was registered,
  no image was generated, and no user job was interrupted by these checks.

The unit sets `MemoryMax=12G`, `MemorySwapMax=0`, `CPUQuota=400%`, `TasksMax=128`,
`NoNewPrivileges=yes`, and control-group termination. The deployment owner must
write the private credential environment before enabling the service and must
deploy Studio's matching flock implementation before accepting image work.
