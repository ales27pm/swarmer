from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import media_contract as contract
import media_runtime as runtime
import media_worker as worker
import render


def audio_args(**changes):
    return {
        "text": "Bonjour, c'est l'été.\nÀ bientôt !",
        "language": "fr-FR",
        "voice": "ff_siwis",
        "max_duration_seconds": 30,
        **changes,
    }


def image_args(**changes):
    return {
        "prompt": "Un paysage d'hiver",
        "width": 512,
        "height": 512,
        "steps": 4,
        "seed": 12,
        **changes,
    }


def write_wav(path, frames=24_001, rate=24_000, channels=1):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x00\x00" * frames * channels)


@pytest.fixture
def profile(tmp_path):
    values = {
        "config.json": b"{}",
        "kokoro-v1_0.pth": b"trusted-model",
        "voices/ff_siwis.pt": b"trusted-voice",
    }
    for name, data in values.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    value = {
        "schema_version": "1.0",
        "backend": "kokoro",
        "origins": [{"repo_id": "hexgrad/Kokoro-82M", "revision": "a" * 40}],
        "components": {
            "config": "config.json",
            "weights": "kokoro-v1_0.pth",
            "voice": "voices/ff_siwis.pt",
        },
        "files": [
            {
                "path": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in values.items()
        ],
        "steps": None,
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    return path


def test_payload_preserves_exact_text_and_context_cannot_override_runtime():
    original = audio_args(
        context={"goal_id": "goal_1", "text": "ignore", "model": "other"}
    )
    result = contract.validate_payload("audio.synthesize", original)
    assert result == audio_args()
    assert original["context"]["text"] == "ignore"


@pytest.mark.parametrize(
    "changes",
    [
        {"voice": "unknown"},
        {"language": "fr"},
        {"text": " "},
        {"text": "\0"},
        {"text": "a" * 1001},
        {"max_duration_seconds": True},
        {"max_duration_seconds": 31},
        {"url": "https://example.com/model"},
        {"context": []},
    ],
)
def test_invalid_audio_arguments(changes):
    with pytest.raises(contract.MediaError, match="invalid_arguments"):
        contract.validate_payload("audio.synthesize", audio_args(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 1024},
        {"seed": -1},
        {"steps": True},
        {"steps": 31},
        {"prompt": "a" * 2001},
        {"negative_prompt": ""},
    ],
)
def test_invalid_image_arguments(changes):
    with pytest.raises(contract.MediaError, match="invalid_arguments"):
        contract.validate_payload("image.generate", image_args(**changes))


@pytest.mark.parametrize(
    "source",
    ["Bon matin !  Voilà.\n" * 25, "a" * 1000, "Éléphant; camion? jardin! " * 30],
)
def test_chunking_covers_every_source_character_without_slicing_phonemes(source):
    calls = []

    def phonemize(segment):
        calls.append(segment)
        return segment * 3

    chunks = contract.phoneme_chunks(source, phonemize, maximum=100)
    assert "".join(part for part, _ in chunks) == source
    assert all(phones == part * 3 and len(phones) <= 100 for part, phones in chunks)
    assert all(part in calls for part, _ in chunks)


def test_chunking_rejects_unrepresentable_or_silently_dropped_text():
    with pytest.raises(contract.MediaError, match="invalid_output"):
        contract.phoneme_chunks("Bonjour", lambda _: "")
    with pytest.raises(contract.MediaError, match="invalid_output"):
        contract.phoneme_chunks("a", lambda _: "p" * 501)


def test_profile_integrity_is_streamed_and_checked_before_loading(profile):
    checks = []
    result = contract.verify_profile(
        profile, "audio.synthesize", lambda: checks.append(True)
    )
    assert result["backend"] == "kokoro" and len(checks) >= 6
    (profile.parent / "voices/ff_siwis.pt").write_bytes(b"changed-voice")
    with pytest.raises(contract.MediaError, match="model_integrity_error"):
        contract.verify_profile(profile, "audio.synthesize")


@pytest.mark.parametrize(
    "path",
    [
        "../model.pt",
        "/model.pt",
        "a//b.pt",
        "a/./b.pt",
        "a/../../b.pt",
        "https://site/weights.pt",
        "a\\b.pt",
    ],
)
def test_manifest_rejects_traversal(path):
    assert not contract.safe_relative(path)


def test_profile_symlink_and_unpinned_component_fail(profile):
    link = profile.with_name("alias.json")
    link.symlink_to(profile)
    with pytest.raises(contract.MediaError, match="model_unavailable"):
        contract.verify_profile(link, "audio.synthesize")
    value = json.loads(profile.read_text())
    value["components"]["voice"] = "unknown.pt"
    profile.write_text(json.dumps(value))
    with pytest.raises(contract.MediaError, match="model_integrity_error"):
        contract.verify_profile(profile, "audio.synthesize")


def test_profile_rejects_weight_symlink_even_to_same_bytes(profile):
    voice = profile.parent / "voices/ff_siwis.pt"
    other = profile.parent / "backup.pt"
    voice.rename(other)
    voice.symlink_to(other)
    with pytest.raises(contract.MediaError, match="model_integrity_error"):
        contract.verify_profile(profile, "audio.synthesize")


def test_image_manifest_must_list_every_local_loader_file(profile):
    value = json.loads(profile.read_text())
    value.update(
        backend="sdxl-lightning",
        steps=4,
        components={"base": "base", "unet": "lightning.safetensors"},
    )
    values = {
        "base/model_index.json": b"{}",
        "base/unet/config.json": b"{}",
        "lightning.safetensors": b"local-unet",
    }
    value["files"] = []
    for name, data in values.items():
        path = profile.parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        value["files"].append(
            {
                "path": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    profile.write_text(json.dumps(value))
    assert contract.verify_profile(profile, "image.generate")["steps"] == 4
    (profile.parent / "base/unpinned.json").write_text("{}")
    with pytest.raises(contract.MediaError, match="model_integrity_error"):
        contract.verify_profile(profile, "image.generate")


def test_real_png_and_pcm_wav_are_checked_and_hashed(tmp_path):
    png = tmp_path / "out.png"
    Image.new("RGB", (512, 512), "blue").save(png)
    image = contract.validate_media(png, "image.generate", image_args())
    assert (
        image["width"] == 512
        and image["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()
    )
    wav = tmp_path / "out.wav"
    write_wav(wav)
    audio = contract.validate_media(wav, "audio.synthesize", audio_args())
    assert audio["duration_ms"] == 1001 and audio["sample_rate"] == 24_000


@pytest.mark.parametrize(
    "rate,channels,frames,error",
    [
        (44_100, 1, 10, "invalid_output"),
        (24_000, 2, 10, "invalid_output"),
        (24_000, 1, 0, "invalid_output"),
        (24_000, 1, 24_001, "duration_limit"),
    ],
)
def test_wav_does_not_accept_wrong_format_or_truncate_duration(
    tmp_path, rate, channels, frames, error
):
    path = tmp_path / "bad.wav"
    write_wav(path, frames=frames, rate=rate, channels=channels)
    with pytest.raises(contract.MediaError, match=error):
        contract.validate_media(
            path, "audio.synthesize", audio_args(max_duration_seconds=1)
        )


def test_truncated_wav_and_wrong_png_dimensions_are_rejected(tmp_path):
    path = tmp_path / "bad.wav"
    write_wav(path)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(contract.MediaError, match="invalid_output"):
        contract.validate_media(path, "audio.synthesize", audio_args())
    Image.new("RGB", (512, 768)).save(tmp_path / "bad.png")
    with pytest.raises(contract.MediaError, match="invalid_output"):
        contract.validate_media(tmp_path / "bad.png", "image.generate", image_args())


class FakeHeartbeat:
    def __init__(self, client, job_id, lease, interval):
        self.client = client
        self.stopped = False
        client.heartbeat = self

    def start(self):
        self.client.events.append("renewal_started")

    def ensure_active(self):
        if self.client.lost:
            raise worker.protocol.LeaseLost("cancelled")

    def stop(self):
        self.stopped = True


class FakeClient:
    def __init__(self, skill="audio.synthesize"):
        self.job = {
            "id": "job_abc",
            "required_skill": skill,
            "claim_token": "test-token",
            "lease_id": "lease_1",
            "lease_generation": 2,
            "payload": {
                **(audio_args() if skill == "audio.synthesize" else image_args()),
                "context": {"goal_id": "goal_abc"},
            },
        }
        self.events = []
        self.results = []
        self.lost = False
        self.lose_on_upload = False
        self.bad_receipt = False

    def heartbeat_agent(self, status):
        self.events.append(status)

    def claim(self):
        self.events.append("claim")
        return copy.deepcopy(self.job)

    def heartbeat_job(self, job_id, lease):
        assert lease.body() == {
            "claim_token": "test-token",
            "lease_id": "lease_1",
            "lease_generation": 2,
        }
        self.events.append("renew")

    def upload_media(self, job_id, lease, path, metadata):
        assert path.read_bytes().startswith(
            b"RIFF" if self.job["required_skill"] == "audio.synthesize" else b"\x89PNG"
        )
        self.events.append("upload")
        self.lost = self.lose_on_upload
        return {
            "artifact_id": "media_a",
            "job_id": "different" if self.bad_receipt else job_id,
            "goal_id": "goal_abc",
            **metadata,
        }

    def submit_result(self, job_id, lease, result):
        self.results.append(result)
        self.events.append("result")


class FakeRenderer:
    def acquire_slot(self):
        return True

    def release_slot(self):
        pass

    def __init__(self, skill="audio.synthesize", *, available=True, failure=None):
        self.skill, self.ready, self.failure = skill, available, failure
        self.paths = []
        self.unavailable_status = "online"

    def available(self):
        return self.ready

    def generate(self, payload, directory, ensure):
        ensure()
        if self.failure:
            raise contract.MediaError(self.failure)
        path = directory / (
            "output.wav" if self.skill == "audio.synthesize" else "output.png"
        )
        self.paths.append(path)
        if self.skill == "audio.synthesize":
            write_wav(path)
        else:
            Image.new("RGB", (512, 512)).save(path)
        return path


@pytest.mark.parametrize("skill", ["audio.synthesize", "image.generate"])
def test_protocol_uploads_real_binary_then_submits_only_server_reference(skill):
    client, renderer = FakeClient(skill), FakeRenderer(skill)
    assert worker.run_once(client, renderer, heartbeat_factory=FakeHeartbeat)
    assert (
        client.events.index("renew")
        < client.events.index("upload")
        < client.events.index("result")
    )
    result = client.results[0]
    assert set(result) == {"status", "result"} and result["status"] == "completed"
    assert set(result["result"]) == {"schema_version", "content_trust", "artifact"}
    assert result["result"]["content_trust"] == "untrusted"
    assert set(result["result"]["artifact"]) == worker.ARTIFACT_FIELDS
    assert not renderer.paths[0].exists() and client.heartbeat.stopped


def test_gpu_unavailable_before_claim_consumes_no_attempt():
    client = FakeClient("image.generate")
    assert not worker.run_once(
        client,
        FakeRenderer("image.generate", available=False),
        heartbeat_factory=FakeHeartbeat,
    )
    assert client.events == ["online"] and client.results == []


def test_busy_shared_slot_never_claims_and_releases_after_failure():
    client = FakeClient("image.generate")
    renderer = FakeRenderer("image.generate")
    released = []
    renderer.acquire_slot = lambda: False
    renderer.release_slot = lambda: released.append(True)
    assert not worker.run_once(client, renderer, heartbeat_factory=FakeHeartbeat)
    assert "claim" not in client.events
    assert released == [True]
    renderer.acquire_slot = lambda: True
    renderer.failure = "runtime_error"
    assert worker.run_once(client, renderer, heartbeat_factory=FakeHeartbeat)
    assert client.results[-1]["error"] == "runtime_error"
    assert released == [True, True]


def test_cancellation_after_upload_discards_result_and_scratch():
    client, renderer = FakeClient(), FakeRenderer()
    client.lose_on_upload = True
    assert worker.run_once(client, renderer, heartbeat_factory=FakeHeartbeat)
    assert "upload" in client.events and not client.results
    assert not renderer.paths[0].exists() and client.heartbeat.stopped


def test_cancellation_before_render_does_not_upload_or_report_failure():
    client = FakeClient()
    client.lost = True
    assert worker.run_once(client, FakeRenderer(), heartbeat_factory=FakeHeartbeat)
    assert "upload" not in client.events and not client.results


def test_wrong_artifact_job_never_completes():
    client = FakeClient()
    client.bad_receipt = True
    assert not worker.run_once(client, FakeRenderer(), heartbeat_factory=FakeHeartbeat)
    assert not client.results


def test_failed_renderer_submits_only_fixed_code(caplog):
    client = FakeClient()
    assert worker.run_once(
        client, FakeRenderer(failure="duration_limit"), heartbeat_factory=FakeHeartbeat
    )
    assert client.results == [{"status": "failed", "error": "duration_limit"}]
    assert (
        "upload" not in client.events
        and client.job["payload"]["text"] not in caplog.text
    )


def test_upload_binary_auth_and_lease_are_fenced(monkeypatch, tmp_path):
    path = tmp_path / "output.wav"
    write_wav(path)
    metadata = contract.validate_media(path, "audio.synthesize", audio_args())
    client = worker.MediaClient("http://127.0.0.1:8710", "agent_a", "test-credential")
    lease = worker.protocol.LeaseProof.from_job(FakeClient().job)
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b'{"artifact_id":"media_a"}')

    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    assert client.upload_media("job_abc", lease, path, metadata) == {
        "artifact_id": "media_a"
    }
    request = requests[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:8710/agents/agent_a/jobs/job_abc/media"
    assert (
        headers["x-claim-token"] == "test-token"
        and headers["x-lease-generation"] == "2"
    )
    assert (
        headers["content-type"] == "audio/wav"
        and headers["x-artifact-sha256"] == metadata["sha256"]
    )
    assert request.data == path.read_bytes()
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    with pytest.raises(contract.MediaError, match="invalid_output"):
        client.upload_media("job_abc", lease, path, metadata)
    assert len(requests) == 1


def test_upload_stale_lease_rejected(monkeypatch, tmp_path):
    path = tmp_path / "output.wav"
    write_wav(path)

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 409, "stale", {}, None)

    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    client = worker.MediaClient("http://127.0.0.1:8710", "agent_a", "test-credential")
    with pytest.raises(worker.protocol.LeaseLost):
        client.upload_media(
            "job_abc",
            worker.protocol.LeaseProof.from_job(FakeClient().job),
            path,
            contract.validate_media(path, "audio.synthesize", audio_args()),
        )


def test_upload_storage_limit_is_an_explicit_failed_job(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 507, "quota", {}, None)

    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    client = FakeClient()
    actual = worker.MediaClient("http://127.0.0.1:8710", "agent_a", "test-credential")
    client.upload_media = actual.upload_media
    assert worker.run_once(client, FakeRenderer(), heartbeat_factory=FakeHeartbeat)
    assert client.results == [{"status": "failed", "error": "storage_limit"}]


@pytest.mark.parametrize("status", [400, 413, 415, 422])
def test_upload_binary_rejection_is_not_misreported_as_lease_loss(monkeypatch, status):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, status, "bad media", {}, None
            )

    monkeypatch.setattr(
        worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    client = FakeClient()
    actual = worker.MediaClient("http://127.0.0.1:8710", "agent_a", "test-credential")
    client.upload_media = actual.upload_media
    assert worker.run_once(client, FakeRenderer(), heartbeat_factory=FakeHeartbeat)
    assert client.results == [{"status": "failed", "error": "invalid_output"}]


@pytest.mark.parametrize(
    "body,expected",
    [
        (b'{"models":[]}', False),
        (b'{"models":[{"size_vram":0}]}', False),
        (b'{"models":[{"size_vram":1}]}', True),
        (b'{"models":[{}]}', True),
        (b"{}", True),
        (b"bad", True),
    ],
)
def test_ollama_residency_fails_closed(monkeypatch, body, expected):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(body)

    monkeypatch.setattr(
        runtime.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    assert runtime.resident_gpu_models() is expected
    assert requests[0].get_method() == "GET" and requests[0].full_url.endswith(
        "/api/ps"
    )


def test_audio_does_not_consult_gpu_and_image_does(profile):
    calls = []
    busy = lambda: calls.append(True) or True
    assert runtime.MediaRenderer(
        profile, "audio.synthesize", residency=busy
    ).available()
    assert calls == []
    assert not runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=busy
    ).available()
    assert calls == [True]
    disabled = runtime.MediaRenderer(profile, "image.generate", residency=busy)
    assert not disabled.available() and disabled.unavailable_status == "busy"
    enabled = runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=busy
    )
    assert enabled.unavailable_status == "online"
    assert enabled.max_rss == 12 * 1024**3


def test_child_is_stopped_on_wall_timeout_and_lease_cancellation(
    monkeypatch, profile, tmp_path
):
    real_popen = subprocess.Popen
    children = []

    def sleeping_child(command, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        children.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", sleeping_child)
    renderer = runtime.MediaRenderer(profile, "audio.synthesize", timeout_seconds=1)
    with pytest.raises(contract.MediaError, match="wall_timeout"):
        renderer.generate(audio_args(), tmp_path, lambda: None)
    assert children[0].poll() is not None

    def cancel_after_start():
        if len(children) > 1:
            raise worker.protocol.LeaseLost("cancelled")

    with pytest.raises(worker.protocol.LeaseLost):
        renderer.generate(audio_args(), tmp_path, cancel_after_start)
    assert children[1].poll() is not None


def test_bad_child_error_cannot_escape_as_arbitrary_string():
    assert contract.MediaError({"secret": "hidden"}).code == "runtime_error"
    assert contract.MediaError("sensitive freeform error").code == "runtime_error"


def test_renderer_offline_network_guard_runs_in_a_separate_process():
    source = "import sys,socket;sys.path.insert(0,sys.argv[1]);from render import deny_network;sys.addaudithook(deny_network);socket.create_connection(('127.0.0.1',1))"
    result = subprocess.run(
        [sys.executable, "-c", source, str(Path(__file__).parent)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0 and b"MediaError: runtime_error" in result.stderr


def test_image_residency_is_rechecked_after_claim_without_starting_child(
    monkeypatch, profile, tmp_path
):
    checks = iter([False, True])
    renderer = runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=lambda: next(checks)
    )
    assert renderer.available()
    monkeypatch.setattr(
        runtime,
        "verify_profile",
        lambda *args: {"steps": 4, "backend": "sdxl-lightning"},
    )

    def forbidden_child(*args, **kwargs):
        pytest.fail("must not start CUDA while residency is occupied")

    monkeypatch.setattr(runtime.subprocess, "Popen", forbidden_child)
    with pytest.raises(contract.MediaError, match="resource_busy"):
        renderer.generate(image_args(), tmp_path, lambda: None)


def test_lightning_checkpoint_step_mismatch_never_starts_child(
    monkeypatch, profile, tmp_path
):
    renderer = runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=lambda: False
    )
    monkeypatch.setattr(
        runtime,
        "verify_profile",
        lambda *args: {"steps": 4, "backend": "sdxl-lightning"},
    )
    with pytest.raises(contract.MediaError, match="unsupported_steps"):
        renderer.generate(image_args(steps=3), tmp_path, lambda: None)
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize("checkpoint_dtype", ["fp16", "fp32"])
def test_image_uses_empty_initialization_and_fp16_storage_assignment(
    monkeypatch, tmp_path, checkpoint_dtype
):
    # No CUDA claim: fake libraries verify the memory-sensitive loading API contract.
    events = []
    tensor = SimpleNamespace(
        dtype=checkpoint_dtype, is_floating_point=lambda: True, is_meta=False
    )

    @contextlib.contextmanager
    def empty_weights(**kwargs):
        assert kwargs == {"include_buffers": False}
        events.append("enter_empty")
        yield
        events.append("leave_empty")

    class UNet:
        @staticmethod
        def from_config(config):
            assert events[-1] == "enter_empty"
            return UNet()

        def load_state_dict(self, weights, *, strict, assign):
            assert strict and assign and weights == {"weight": tensor}
            events.append("assign_fp16")

        def parameters(self):
            return [tensor]

        def buffers(self):
            return []

        def eval(self):
            return self

    class Pipeline:
        scheduler = SimpleNamespace(config={})

        @staticmethod
        def from_pretrained(path, **kwargs):
            assert events[-1] == "assign_fp16"
            assert (
                kwargs["low_cpu_mem_usage"]
                and kwargs["local_files_only"]
                and kwargs["use_safetensors"]
            )
            assert kwargs["torch_dtype"] == "fp16" and kwargs["variant"] == "fp16"
            return Pipeline()

        def enable_attention_slicing(self, mode):
            assert mode == "max"

        def enable_vae_tiling(self):
            pass

        def enable_model_cpu_offload(self):
            pass

        def __call__(self, **kwargs):
            assert kwargs["num_inference_steps"] == 4 and kwargs["guidance_scale"] == 0
            assert kwargs["prompt"] == image_args()["prompt"]
            return SimpleNamespace(images=[Image.new("RGB", (512, 512))])

    fake_torch = SimpleNamespace(
        float16="fp16",
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_capability=lambda: (7, 5),
            mem_get_info=lambda: (4 * 1024**3, 8 * 1024**3),
        ),
        set_num_threads=lambda value: None,
        backends=SimpleNamespace(
            cuda=SimpleNamespace(
                enable_flash_sdp=lambda value: events.append(("flash", value)),
                enable_mem_efficient_sdp=lambda value: None,
                enable_math_sdp=lambda value: None,
            )
        ),
        Generator=lambda **kwargs: SimpleNamespace(manual_seed=lambda seed: seed),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(
        sys.modules, "accelerate", SimpleNamespace(init_empty_weights=empty_weights)
    )
    monkeypatch.setitem(
        sys.modules,
        "diffusers",
        SimpleNamespace(
            UNet2DConditionModel=UNet,
            StableDiffusionXLPipeline=Pipeline,
            EulerDiscreteScheduler=SimpleNamespace(
                from_config=lambda config, **kwargs: None
            ),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "safetensors.torch",
        SimpleNamespace(load_file=lambda *args, **kwargs: {"weight": tensor}),
    )
    (tmp_path / "base/unet").mkdir(parents=True)
    (tmp_path / "base/unet/config.json").write_text("{}")
    profile = {
        "steps": 4,
        "components": {"base": "base", "unet": "lightning.safetensors"},
    }
    if checkpoint_dtype == "fp32":
        with pytest.raises(contract.MediaError, match="model_integrity_error"):
            render.image(profile, tmp_path, image_args(), tmp_path / "out.png")
        assert "assign_fp16" not in events
    else:
        render.image(profile, tmp_path, image_args(), tmp_path / "out.png")
        assert (
            events.index("enter_empty")
            < events.index("leave_empty")
            < events.index("assign_fp16")
        )
        assert ("flash", False) in events
        assert (
            contract.validate_media(
                tmp_path / "out.png", "image.generate", image_args()
            )["width"]
            == 512
        )
