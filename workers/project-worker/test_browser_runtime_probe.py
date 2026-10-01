"""Local guard tests. Real browser proof is setup_browser_runtime.py build/qualify."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("setup_browser_runtime")
probe = load("browser_runtime_probe")


def sandbox_text(path, routes=""):
    name = str(path)
    if name.endswith("status"):
        return "NoNewPrivs:\t1\nCapEff:\t00000000\nSeccomp:\t2\n"
    if name.endswith("attr/current"):
        return "docker-default (enforce)\n"
    if name.endswith("pids.max"):
        return "64\n"
    return "Iface Destination Gateway\n" + routes


def test_exact_worker_limits_and_no_sandbox_relaxation():
    command = setup.probe_command(
        "sha256:" + "a" * 64,
        "swarmer-browser-proof-" + "b" * 32 + "-selenium",
        "selenium",
        1000,
        1000,
    )
    for key, value in {
        "--network": "none",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--pids-limit": "64",
        "--memory": "1g",
        "--memory-swap": "1g",
        "--cpus": "2",
        "--user": "1000:1000",
        "--pull": "never",
    }.items():
        assert command[command.index(key) + 1] == value
    assert "--read-only" in command
    assert "nofile=256:256" in command
    assert not any(
        "no-sandbox" in item or "privileged" in item or "docker.sock" in item
        for item in command
    )


@pytest.mark.parametrize(
    "image,uid,engine",
    [
        ("latest", 1000, "selenium"),
        ("sha256:" + "a" * 64, 0, "selenium"),
        ("sha256:" + "a" * 64, 1000, "arbitrary"),
    ],
)
def test_bad_probe_inputs_rejected(image, uid, engine):
    with pytest.raises(ValueError):
        setup.probe_command(
            image, "swarmer-browser-proof-" + "b" * 32 + "-selenium", engine, uid, 1000
        )


def test_foreign_container_cleanup_name_rejected():
    with pytest.raises(ValueError):
        setup.probe_command(
            "sha256:" + "a" * 64, "swarmer-production", "selenium", 1000, 1000
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("base_image", "latest"),
        ("snapshot", "latest"),
        ("repositories", ["https://arbitrary.invalid"]),
        ("apt_packages", ["curl"]),
        ("platform", "linux/arm64"),
    ],
)
def test_lock_rejects_mutable_or_unexpected_inputs(tmp_path, key, value):
    data = json.loads((ROOT / "browser-system.lock.json").read_text())
    data[key] = value
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        setup.load_lock(path)


def test_snapshot_lock_accepts_reviewed_frozen_scope():
    assert (
        setup.load_lock(ROOT / "browser-system.lock.json")["snapshot"]
        == "20260930T000000Z"
    )


@pytest.mark.parametrize(
    "exception,expected",
    [
        (RuntimeError("secret text Operation not permitted"), "browser_sandbox_denied"),
        (RuntimeError("SECRET_NO_PRINT"), "browser_launch_or_protocol_failed"),
        (AssertionError("private details"), "browser_assertion_failed"),
    ],
)
def test_diagnostics_closed(exception, expected):
    assert probe.closed_failure(exception) == expected
    assert "secret" not in probe.closed_failure(exception)


def test_root_precondition_rejected(monkeypatch):
    monkeypatch.setattr(probe.os, "getuid", lambda: 0)
    monkeypatch.setattr(probe.Path, "read_text", sandbox_text)

    class Stats:
        f_flag = probe.os.ST_RDONLY

    monkeypatch.setattr(probe.os, "statvfs", lambda _: Stats())
    with pytest.raises(RuntimeError, match="sandbox_precondition_failed"):
        probe.sandbox_proof()


@pytest.mark.parametrize(
    "interfaces,routes", [(["lo", "eth0"], ""), (["lo"], "eth0 00000000 00000000")]
)
def test_external_network_shape_rejected_even_if_testnet_would_fail(
    monkeypatch, interfaces, routes
):
    monkeypatch.setattr(probe.os, "getuid", lambda: 1000)
    monkeypatch.setattr(
        probe.Path,
        "read_text",
        lambda p: sandbox_text(p, routes),
    )
    monkeypatch.setattr(
        probe.Path, "iterdir", lambda _: [Path(name) for name in interfaces]
    )

    class Stats:
        f_flag = probe.os.ST_RDONLY

    monkeypatch.setattr(probe.os, "statvfs", lambda _: Stats())
    with pytest.raises(RuntimeError, match="network_namespace_not_isolated"):
        probe.sandbox_proof()


def test_loopback_shape_is_proof_and_testnet_failure_only_complement(monkeypatch):
    monkeypatch.setattr(probe.os, "getuid", lambda: 1000)
    monkeypatch.setattr(
        probe.Path,
        "read_text",
        sandbox_text,
    )
    monkeypatch.setattr(probe.Path, "iterdir", lambda _: [Path("lo")])

    class Stats:
        f_flag = probe.os.ST_RDONLY

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _seconds):
            pass

        def connect(self, _address):
            raise OSError("unreachable")

    monkeypatch.setattr(probe.os, "statvfs", lambda _: Stats())
    monkeypatch.setattr(probe.socket, "socket", Socket)
    result = probe.sandbox_proof()
    assert result["network_interfaces"] == ["lo"]
    assert result["non_loopback_ipv4_routes"] == 0
    assert result["testnet_connect_failed_only"] is True
    assert "external_connect_blocked" not in result


def test_unproven_success_receipt_rejected():
    with pytest.raises(ValueError, match="unproven_success_receipt"):
        setup.validate_pass_receipt({"status": "passed"}, "selenium", 1000)


def test_timeout_records_bounded_failure_and_only_cleans_owned_names(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "run":
            raise setup.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    result = setup.qualify("sha256:" + "a" * 64, tmp_path)
    assert result["status"] == "blocked"
    assert [r["failure_code"] for r in result["probes"]] == ["probe_outer_timeout"] * 2
    assert all(kwargs["timeout"] == 45 for argv, kwargs in calls if argv[1] == "run")
    assert len([argv for argv, _ in calls if argv[1:3] == ["rm", "-f"]]) == 2
    assert all(
        argv[-1].startswith("swarmer-browser-proof-")
        for argv, _ in calls
        if argv[1] == "rm"
    )


def test_private_native_log_is_separate_from_public_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)

    class Result:
        returncode = 1
        stdout = json.dumps(
            {
                "status": "failed",
                "failure_code": "browser_sandbox_denied",
                "_private_native_logs": "PRIVATE_NATIVE_DIAGNOSTIC",
            }
        )

    monkeypatch.setattr(setup.subprocess, "run", lambda *_args, **_kwargs: Result())
    result = setup.qualify("sha256:" + "a" * 64, tmp_path)
    assert "PRIVATE_NATIVE_DIAGNOSTIC" not in json.dumps(result)
    assert (
        "PRIVATE_NATIVE_DIAGNOSTIC" not in (tmp_path / "qualification.json").read_text()
    )
    assert (tmp_path / "selenium-native.log").read_text() == "PRIVATE_NATIVE_DIAGNOSTIC"
    assert (tmp_path / "selenium-native.log").stat().st_mode & 0o777 == 0o600


def test_install_rejects_host_invocation_before_commands(monkeypatch):
    monkeypatch.setattr(setup, "load_lock", lambda _path: {})
    monkeypatch.setattr(setup.os, "getuid", lambda: 0)
    monkeypatch.delenv("SWARMER_BROWSER_IMAGE_BUILD", raising=False)
    monkeypatch.setattr(
        setup, "run", lambda *_args, **_kwargs: pytest.fail("must not invoke apt/pip")
    )
    with pytest.raises(RuntimeError, match="installation_requires_candidate_container"):
        setup.install_image()


def test_overlay_rejects_changed_dependency_before_tag_or_build(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return json.dumps({"input_sha256": {"browser-system.lock.json": "wrong"}})

    monkeypatch.setattr(setup, "run", fake_run)
    with pytest.raises(
        ValueError, match="overlay_dependency_changed_requires_full_build"
    ):
        setup.overlay("sha256:" + "a" * 64, tmp_path / "overlay")
    assert len(calls) == 1
    assert calls[0][1] == "run" and "--read-only" in calls[0]


def test_profile_keeps_exact_upstream_and_namespace_subset():
    profile = json.loads((ROOT / setup.PROFILE_NAME).read_text())
    provenance = json.loads((ROOT / "browser-seccomp-v1.provenance.json").read_text())
    base = dict(profile, syscalls=profile["syscalls"][:33])
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    assert (
        hashlib.sha256(canonical(base)).hexdigest()
        == provenance["upstream_canonical_sha256"]
    )
    assert (
        hashlib.sha256((ROOT / setup.PROFILE_NAME).read_bytes()).hexdigest()
        == setup.PROFILE_SHA
    )
    additions = profile["syscalls"][33:]
    assert additions == provenance["added_rules"]
    assert {name for rule in additions for name in rule["names"]} == {
        "clone",
        "unshare",
        "chroot",
    }
    namespaces = [
        0x20000,
        0x2000000,
        0x4000000,
        0x8000000,
        0x10000000,
        0x20000000,
        0x40000000,
    ]
    mask = additions[0]["args"][0]["value"]
    for subset in range(128):
        flags = sum(flag for i, flag in enumerate(namespaces) if subset & (1 << i))
        assert ((flags & mask) == 0) == ((flags & ~0x70000000) == 0)
    assert all(rule["includes"] == {"arches": ["amd64"]} for rule in additions)


def test_optin_profile_changes_only_explicit_policy(tmp_path):
    path = tmp_path / setup.PROFILE_NAME
    path.write_bytes((ROOT / setup.PROFILE_NAME).read_bytes())
    args = (
        "sha256:" + "a" * 64,
        "swarmer-browser-proof-" + "b" * 32 + "-selenium",
        "selenium",
        1000,
        1000,
    )
    normal = setup.probe_command(*args)
    browser = setup.probe_command(*args, profile=path)
    assert normal[normal.index("--pids-limit") + 1] == "64"
    assert browser[browser.index("--pids-limit") + 1] == "256"
    assert "seccomp=" + str(path) in browser
    assert "HOME=/tmp" in normal and "HOME=/tmp" in browser
    browser[browser.index("--pids-limit") + 1] = "64"
    i = browser.index("seccomp=" + str(path))
    del browser[i - 1 : i + 1]
    assert browser == normal


@pytest.mark.parametrize("kind", ["tampered", "symlink"])
def test_profile_drift_rejected(tmp_path, kind):
    path = tmp_path / setup.PROFILE_NAME
    if kind == "symlink":
        path.symlink_to(ROOT / setup.PROFILE_NAME)
    else:
        path.write_text("{}")
    with pytest.raises(ValueError, match="identity_mismatch"):
        setup.profile_metadata(path)


def test_profile_unsupported_engine_fails_before_publication(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(
        setup,
        "run",
        lambda *_a, **_k: json.dumps(
            {"Version": "29.7.0", "GitCommit": "wrong", "Os": "linux", "Arch": "amd64"}
        ),
    )
    with pytest.raises(ValueError, match="engine_not_qualified"):
        setup.prepare_browser_profile(tmp_path)
    assert not (tmp_path / setup.PROFILE_NAME).exists()


def test_timeout_native_log_is_retained_privately(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)
    def timed(argv, **kwargs):
        if argv[1] == "run":
            raise setup.subprocess.TimeoutExpired(
                argv, kwargs["timeout"], stderr=b"PRIVATE_NATIVE_TIMEOUT"
            )

    monkeypatch.setattr(setup.subprocess, "run", timed)
    result = setup.qualify("sha256:" + "a" * 64, tmp_path)
    assert "PRIVATE_NATIVE_TIMEOUT" not in json.dumps(result)
    assert (
        tmp_path / "playwright-transport.log"
    ).read_text() == "PRIVATE_NATIVE_TIMEOUT"
    assert (tmp_path / "playwright-transport.log").stat().st_mode & 0o777 == 0o600
