"""Private offline render entry point. Arguments are written by the fixed worker only."""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import wave
from pathlib import Path

from media_contract import (
    MAX_MEDIA_BYTES,
    MediaError,
    digest_file,
    local_path,
    phoneme_chunks,
    validate_media,
    validate_payload,
)


def deny_network(event: str, _arguments: object) -> None:
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "urllib.Request",
    }:
        raise MediaError("runtime_error")


def synthesize(profile: dict, root: Path, payload: dict, output: Path) -> None:
    import espeakng_loader
    import numpy as np
    import torch
    from kokoro import KModel
    from misaki.espeak import EspeakG2P
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    # The isolated wheel supplies both the library and its language data; no apt/global install.
    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    components = profile["components"]
    model = (
        KModel(
            repo_id="hexgrad/Kokoro-82M",
            config=str(local_path(root, components["config"])),
            model=str(local_path(root, components["weights"])),
        )
        .to("cpu")
        .eval()
    )
    voice = torch.load(
        local_path(root, components["voice"]), map_location="cpu", weights_only=True
    )
    g2p = EspeakG2P(language="fr-fr")
    chunks = phoneme_chunks(payload["text"], lambda segment: g2p(segment)[0])
    frames = 0
    with wave.open(str(output), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        for _, phones in chunks:
            if not phones:
                continue
            # KModel silently drops unknown symbols, so reject them before calling it.
            if (
                any(symbol not in model.vocab for symbol in phones)
                or len(phones) + 2 > model.context_length
                or len(phones) > len(voice)
            ):
                raise MediaError("invalid_output")
            with torch.inference_mode():
                audio = (
                    model(phones, voice[len(phones) - 1], speed=1)
                    .detach()
                    .cpu()
                    .numpy()
                )
            if audio.ndim != 1 or audio.size == 0 or not np.isfinite(audio).all():
                raise MediaError("invalid_output")
            frames += int(audio.size)
            if frames > payload["max_duration_seconds"] * 24_000:
                raise MediaError("duration_limit")
            pcm = (np.clip(audio, -1, 1) * 32767).round().astype("<i2")
            target.writeframes(pcm.tobytes())


def chroma_command(profile: dict, root: Path, payload: dict, output: Path) -> list[str]:
    """The qualified native recipe; only prompt and seed vary between requests."""
    payload = validate_payload("image.generate", payload)
    if (
        profile["backend"] != "chroma-sd-cpp"
        or payload.get("model_profile") != "chroma1-hd-q4"
        or profile["steps"] != 40
    ):
        raise MediaError("model_unavailable")
    components = profile["components"]
    return [
        str(local_path(root, components["runtime"])),
        "--diffusion-model",
        str(local_path(root, components["diffusion"])),
        "--t5xxl",
        str(local_path(root, components["text_encoder"])),
        "--vae",
        str(local_path(root, components["vae"])),
        "--backend",
        "diffusion=cuda0,te=cpu,vae=cpu",
        "--params-backend",
        "diffusion=cuda0,te=cpu,vae=cpu",
        "--max-vram",
        "cuda0=6.5",
        "--diffusion-fa",
        "--steps",
        "40",
        "--cfg-scale",
        "3",
        "--sampling-method",
        "euler",
        "--scheduler",
        "flux",
        "--extra-sample-args",
        "base_shift=1.0986122886681098,max_shift=1.0986122886681098",
        "-W",
        "512",
        "-H",
        "512",
        "-b",
        "1",
        "-s",
        str(payload["seed"]),
        "-t",
        "4",
        "-p",
        payload["prompt"],
        "-n",
        "blurry, low quality",
        "-o",
        str(output),
    ]


def chroma_image(profile: dict, root: Path, payload: dict, output: Path) -> None:
    # Deliberately stay in the parent renderer's process group. Its inclusive
    # deadline, RSS limit and lease cancellation terminate native descendants too.
    result = subprocess.run(
        chroma_command(profile, root, payload, output),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise MediaError("runtime_error")
    validate_media(output, "image.generate", payload)


def image(profile: dict, root: Path, payload: dict, output: Path) -> None:
    if profile.get("backend") == "chroma-sd-cpp":
        chroma_image(profile, root, payload, output)
        return
    import torch
    from accelerate import init_empty_weights
    from diffusers import (
        EulerDiscreteScheduler,
        StableDiffusionXLPipeline,
        UNet2DConditionModel,
    )
    from safetensors.torch import load_file

    if not torch.cuda.is_available() or torch.cuda.get_device_capability() < (7, 5):
        raise MediaError("resource_busy")
    if torch.cuda.mem_get_info()[0] < 3 * 1024**3:
        raise MediaError("resource_busy")
    if payload["steps"] != 4 or profile["steps"] != 4:
        raise MediaError("unsupported_steps")
    torch.set_num_threads(2)
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(False)
    torch.backends.cuda.enable_math_sdp(True)
    components = profile["components"]
    base = local_path(root, components["base"])
    config = json.loads((base / "unet" / "config.json").read_text())
    # Never allocate a full FP32 UNet before loading the ~5 GiB FP16 checkpoint.
    # Tiny buffers remain real; parameters are meta until assigned checkpoint storage.
    with init_empty_weights(include_buffers=False):
        unet = UNet2DConditionModel.from_config(config)
    weights = load_file(str(local_path(root, components["unet"])), device="cpu")
    if any(
        tensor.is_floating_point() and tensor.dtype != torch.float16
        for tensor in weights.values()
    ):
        raise MediaError("model_integrity_error")
    unet.load_state_dict(weights, strict=True, assign=True)
    del weights
    if any(tensor.is_meta for tensor in (*unet.parameters(), *unet.buffers())):
        raise MediaError("model_integrity_error")
    unet.eval()
    pipeline = StableDiffusionXLPipeline.from_pretrained(
        str(base),
        unet=unet,
        torch_dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
        local_files_only=True,
        low_cpu_mem_usage=True,
    )
    pipeline.scheduler = EulerDiscreteScheduler.from_config(
        pipeline.scheduler.config, timestep_spacing="trailing"
    )
    pipeline.enable_attention_slicing("max")
    pipeline.enable_vae_tiling()
    pipeline.enable_model_cpu_offload()
    generator = torch.Generator(device="cpu").manual_seed(payload["seed"])
    result = pipeline(
        prompt=payload["prompt"],
        width=payload["width"],
        height=payload["height"],
        num_inference_steps=4,
        guidance_scale=0.0,
        generator=generator,
    )
    if len(result.images) != 1:
        raise MediaError("invalid_output")
    result.images[0].convert("RGB").save(output, format="PNG")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument(
        "--skill", required=True, choices=["audio.synthesize", "image.generate"]
    )
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    sys.addaudithook(deny_network)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_MEDIA_BYTES, MAX_MEDIA_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    output = args.directory / (
        "output.wav" if args.skill == "audio.synthesize" else "output.png"
    )
    try:
        if digest_file(args.profile) != args.profile_sha256:
            raise MediaError("model_integrity_error")
        profile = json.loads(args.profile.read_text())
        payload = validate_payload(
            args.skill, json.loads((args.directory / "request.json").read_text())
        )
        (synthesize if args.skill == "audio.synthesize" else image)(
            profile, args.profile.parent, payload, output
        )
        result = {"outcome": "completed", "error": None}
        code = 0
    except Exception as exc:  # noqa: BLE001 - child boundary exports only fixed error codes, never library text.
        output.unlink(missing_ok=True)
        result = {
            "outcome": "failed",
            "error": exc.code if isinstance(exc, MediaError) else "runtime_error",
        }
        code = 1
    receipt = args.directory / "render-result.json"
    receipt.write_text(json.dumps(result))
    receipt.chmod(0o600)
    os._exit(code)


if __name__ == "__main__":
    main()
