"""Native adapter boundaries; tiny stand-ins never load GPU weights."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import media_contract as contract
import media_runtime as runtime
import media_worker as worker
import render


def test_shared_gpu_slot_excludes_other_process_and_recovers(tmp_path):
    lock = tmp_path / "image-generation.lock"
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=lock
    )
    assert renderer.acquire_slot()
    script = "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    blocked = subprocess.run(
        [sys.executable, "-c", script, str(lock)], capture_output=True, check=False
    )
    assert blocked.returncode != 0
    renderer.release_slot()
    available = subprocess.run(
        [sys.executable, "-c", script, str(lock)], capture_output=True, check=False
    )
    assert available.returncode == 0


def test_gpu_slot_rejects_symlink_and_busy_owner(tmp_path):
    target = tmp_path / "target"
    target.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=alias
    )
    assert not renderer.acquire_slot()
    renderer.gpu_lock_path = target
    with target.open("r+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not renderer.acquire_slot()
    assert renderer.acquire_slot()
    renderer.release_slot()


def test_nonempty_gpu_slot_marker_is_never_cleared_or_admitted(tmp_path):
    lock = tmp_path / "image-generation.lock"
    marker = b"chroma-studio-v1:operator-scope\n"
    lock.write_bytes(marker)
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=lock
    )
    assert not renderer.acquire_slot()
    assert renderer._gpu_lock_fd is None
    assert lock.read_bytes() == marker
    # Marker refusal must close its descriptor, not retain a hidden flock.
    with lock.open("rb") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_external_admission_probe_releases_slot_for_backend_claim(tmp_path):
    lock = tmp_path / "image-generation.lock"
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=lock,
        external_admission=True,
    )
    assert renderer.probe_slot()
    assert renderer._gpu_lock_fd is None
    with lock.open("rb") as backend:
        fcntl.flock(backend, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not renderer.probe_slot()
    renderer.wait_for_slot(lambda: None)
    assert renderer._gpu_lock_fd is not None
    renderer.release_slot()


def test_external_admission_requires_a_shared_image_lock(tmp_path):
    with pytest.raises(ValueError, match="shared GPU lock"):
        runtime.MediaRenderer(
            tmp_path / "profile.json", "image.generate", external_admission=True
        )
    audio = runtime.MediaRenderer(
        tmp_path / "profile.json", "audio.synthesize", external_admission=True
    )
    assert not audio.external_admission
    assert audio.acquire_slot()


def test_handoff_wait_checks_lease_before_any_acquisition(tmp_path):
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate",
        gpu_lock_path=tmp_path / "lock", external_admission=True,
    )

    def lost():
        raise worker.protocol.LeaseLost("cancelled")

    with pytest.raises(worker.protocol.LeaseLost):
        renderer.wait_for_slot(lost)
    assert renderer._gpu_lock_fd is None


def test_handoff_wait_is_bounded_and_does_not_remove_marker(tmp_path, monkeypatch):
    lock = tmp_path / "lock"
    marker = b"chroma-studio-v1:unfinished\n"
    lock.write_bytes(marker)
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=lock,
        external_admission=True,
    )
    clock = [0.0]
    checks = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(contract.MediaError, match="resource_busy"):
        renderer.wait_for_slot(lambda: checks.append(clock[0]))
    assert 10 <= clock[0] < 10.1
    assert len(checks) > 1
    assert lock.read_bytes() == marker and renderer._gpu_lock_fd is None


def test_handoff_acquires_after_transient_owner_releases(tmp_path, monkeypatch):
    lock = tmp_path / "lock"
    owner = lock.open("a")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json", "image.generate", gpu_lock_path=lock,
        external_admission=True,
    )
    checks = []
    monkeypatch.setattr(runtime.time, "sleep", lambda _delay: owner.close())
    renderer.wait_for_slot(lambda: checks.append(True))
    assert owner.closed and renderer._gpu_lock_fd is not None
    assert len(checks) >= 3
    renderer.release_slot()


@pytest.mark.parametrize("result,expected", [(b"", False), (b"container123\n", True)])
def test_orphan_studio_container_prevents_claim(
    monkeypatch, tmp_path, result, expected
):
    def ps(command, **options):
        assert command == [
            "/usr/bin/docker",
            "ps",
            "--filter",
            "label=app=swarmer-chroma-studio",
            "--format",
            "{{.ID}}",
        ]
        assert options["timeout"] == 3
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(runtime.subprocess, "run", ps)
    renderer = runtime.MediaRenderer(
        tmp_path / "profile.json",
        "image.generate",
        gpu_enabled=True,
        gpu_lock_path=tmp_path / "slot",
        residency=lambda: False,
    )
    assert renderer.available() is (not expected)


def test_unknown_studio_state_fails_closed(monkeypatch):
    def failed(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 3)

    monkeypatch.setattr(runtime.subprocess, "run", failed)
    assert runtime.studio_container_running()


def test_enabled_sandbox_never_falls_back_if_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    with pytest.raises(contract.MediaError, match="runtime_error"):
        runtime.sandbox_command(
            ["python", "render.py"], tmp_path / "profile.json", tmp_path
        )


def image_args(**changes):
    return {
        "prompt": "A red teapot",
        "model_profile": "chroma1-hd-q4",
        "width": 512,
        "height": 512,
        "steps": 40,
        "seed": 42,
        **changes,
    }


def install_profile(tmp_path, executable_source=None):
    """Create a separately hash-pinned fake executable, never claim native inference."""
    value = json.loads(
        (
            Path(__file__).parents[2] / "configs/media/chroma1-hd-q4-sm75-v1.json"
        ).read_text()
    )
    if executable_source is None:
        executable_source = (
            "import sys\nfrom PIL import Image\n"
            "Image.new('RGB',(512,512),'red').save(sys.argv[sys.argv.index('-o')+1])\n"
        )
    for item in value["files"]:
        path = tmp_path / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            f"#!{sys.executable}\n{executable_source}".encode()
            if path.name == "sd-cli"
            else b"fixture only"
        )
        path.write_bytes(data)
        path.chmod(0o700 if path.name == "sd-cli" else 0o400)
        item.update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(value))
    return profile, value


def test_explicit_chroma_contract_and_legacy_default():
    assert contract.validate_payload("image.generate", image_args()) == image_args()
    old = image_args(steps=4)
    del old["model_profile"]
    assert contract.validate_payload("image.generate", old) == old
    for invalid in (
        image_args(steps=4),
        image_args(width=768),
        image_args(height=768),
        image_args(model_profile="unregistered"),
        image_args(model_profile=None),
        image_args(model_profile=[]),
        image_args(steps=True),
    ):
        with pytest.raises(contract.MediaError, match="invalid_arguments"):
            contract.validate_payload("image.generate", invalid)


def test_checked_in_pins_match_qualified_runtime_and_models():
    root = Path(__file__).parents[2]
    value = json.loads((root / "configs/media/chroma1-hd-q4-sm75-v1.json").read_text())
    models = json.loads(
        (
            root / "docs/evidence/chroma-inference-2026-10-02/verified-models.json"
        ).read_text()
    )
    native = json.loads(
        (root / "docs/evidence/chroma-runtime-2026-10-02.json").read_text()
    )
    pinned = {item["sha256"]: item["size_bytes"] for item in value["files"]}
    assert pinned == {
        **{item["verified_sha256"]: item["verified_bytes"] for item in models},
        native["sha256"]: native["bytes"],
    }
    assert value["steps"] == 40


@pytest.mark.parametrize(
    "mutation", ["changed_runtime", "not_executable", "symlink", "extra", "wrong_kind"]
)
def test_chroma_profile_rejects_unpinned_or_wrong_runtime(tmp_path, mutation):
    path, value = install_profile(tmp_path)
    assert contract.verify_profile(path, "image.generate")["backend"] == "chroma-sd-cpp"
    native = tmp_path / value["components"]["runtime"]
    if mutation == "changed_runtime":
        native.write_bytes(b"different executable")
    elif mutation == "not_executable":
        native.chmod(0o600)
    elif mutation == "symlink":
        original = native.with_name("original")
        native.rename(original)
        native.symlink_to(original)
    elif mutation == "extra":
        value["files"].append(
            {"path": "surprise.gguf", "size_bytes": 1, "sha256": "a" * 64}
        )
        path.write_text(json.dumps(value))
    else:
        value["components"]["diffusion"] = value["components"]["vae"]
        path.write_text(json.dumps(value))
    with pytest.raises(contract.MediaError):
        contract.verify_profile(path, "image.generate")


def test_chroma_argv_keeps_prompt_literal_and_qualified_recipe(tmp_path):
    path, value = install_profile(tmp_path)
    prompt = '--output /tmp/evil; $(touch /tmp/injected) "\nα'
    argv = render.chroma_command(
        value, path.parent, image_args(prompt=prompt), tmp_path / "out.png"
    )
    assert argv[argv.index("-p") + 1] == prompt
    assert argv.count(prompt) == 1 and "--output" not in argv
    assert argv[argv.index("--backend") + 1] == "diffusion=cuda0,te=cpu,vae=cpu"
    assert argv[argv.index("--params-backend") + 1] == "diffusion=cuda0,te=cpu,vae=cpu"
    assert (
        argv[argv.index("--extra-sample-args") + 1]
        == "base_shift=1.0986122886681098,max_shift=1.0986122886681098"
    )
    assert argv[argv.index("--steps") + 1] == "40"
    assert argv[argv.index("--cfg-scale") + 1] == "3"
    assert argv[argv.index("--scheduler") + 1] == "flux"
    assert argv[argv.index("--max-vram") + 1] == "cuda0=6.5"
    assert "--diffusion-fa" in argv and "--flow-shift" not in argv


@pytest.mark.parametrize(
    "payload",
    [
        image_args(model_profile="sdxl-lightning-4step", steps=4),
        image_args(width=768),
        image_args(steps=4),
    ],
)
def test_profile_mismatch_never_starts_native_process(tmp_path, monkeypatch, payload):
    path, _ = install_profile(tmp_path)
    renderer = runtime.MediaRenderer(
        path, "image.generate", gpu_enabled=True, residency=lambda: False
    )
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("must not execute")
    )
    with pytest.raises(contract.MediaError):
        renderer.generate(payload, tmp_path, lambda: None)
    assert not (tmp_path / "request.json").exists()


def test_native_adapter_runs_real_subprocess_and_validates_png(tmp_path):
    profile, _ = install_profile(tmp_path)
    renderer = runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=lambda: False
    )
    assert renderer.timeout == 600
    assert runtime.MediaRenderer(profile, "audio.synthesize").timeout == 180
    output = renderer.generate(image_args(), tmp_path, lambda: None)
    assert (
        contract.validate_media(output, "image.generate", image_args())["width"] == 512
    )
    assert json.loads((tmp_path / "render-result.json").read_text()) == {
        "outcome": "completed",
        "error": None,
    }


@pytest.mark.parametrize(
    "source,expected",
    [
        ("raise SystemExit(3)\n", "runtime_error"),
        (
            "import sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('-o')+1]).write_text('not an image')\n",
            "invalid_output",
        ),
    ],
)
def test_native_exit_and_invalid_png_propagate_fixed_failure(
    tmp_path, source, expected
):
    profile, _ = install_profile(tmp_path, source)
    renderer = runtime.MediaRenderer(
        profile, "image.generate", gpu_enabled=True, residency=lambda: False
    )
    with pytest.raises(contract.MediaError, match=expected):
        renderer.generate(image_args(), tmp_path, lambda: None)
    assert not (tmp_path / "output.png").exists()


@pytest.mark.parametrize("cancel", [False, True])
def test_native_descendant_cannot_outlive_deadline_or_cancel(tmp_path, cancel):
    source = (
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('native.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    profile, _ = install_profile(tmp_path, source)
    renderer = runtime.MediaRenderer(
        profile,
        "image.generate",
        gpu_enabled=True,
        residency=lambda: False,
        timeout_seconds=2,
    )

    def ensure_active():
        if cancel and (tmp_path / "native.pid").exists():
            raise worker.protocol.LeaseLost("cancelled")

    expected = worker.protocol.LeaseLost if cancel else contract.MediaError
    with pytest.raises(expected):
        renderer.generate(image_args(), tmp_path, ensure_active)
    assert not (tmp_path / "output.png").exists()
    pid = int((tmp_path / "native.pid").read_text())
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            pytest.fail("native process survived renderer cancellation")
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_profile_models_cannot_be_replaced_by_context(tmp_path):
    path, value = install_profile(tmp_path)
    original = image_args(
        context={"model_profile": "other", "runtime": "/bin/sh", "steps": 1}
    )
    argv = render.chroma_command(value, path.parent, original, tmp_path / "out.png")
    assert argv[0] == str(tmp_path / value["components"]["runtime"])
    assert argv[argv.index("--steps") + 1] == "40"
