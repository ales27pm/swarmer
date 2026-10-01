"""Build and qualify a standalone candidate; never modifies a running service."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

FILES = (
    "Dockerfile.browser",
    "browser-system.lock.json",
    "runtime-browser.lock",
    "runtime-tools.lock",
    "dependency-catalog.json",
    "check_harness.py",
    "setup_browser_runtime.py",
    "browser_runtime_probe.py",
    "browser-seccomp-v1.json",
    "browser-seccomp-v1.provenance.json",
    "browser-seccomp-LICENSE",
)
IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
PROFILE_NAME = "browser-seccomp-v1.json"
PROFILE_SHA = "0f8bd34cf9980268f45c7f0a2a0d5dbff9559b3baf4d3cc4b464872e175a8c88"


def profile_metadata(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or digest(path) != PROFILE_SHA:
        raise ValueError("browser_profile_identity_mismatch")
    return {
        "source_name": PROFILE_NAME,
        "sha256": PROFILE_SHA,
        "mode": "browser-seccomp-v1",
        "path": str(path.resolve()),
        "pids_limit": 256,
    }


def prepare_browser_profile(output: Path) -> dict:
    if (
        output.is_symlink()
        or output.stat().st_uid != os.getuid()
        or output.stat().st_mode & 0o077
    ):
        raise ValueError("browser_profile_requires_private_directory")
    engine = json.loads(
        run(["docker", "version", "--format", "{{json .Server}}"], timeout=10)
    )
    if (
        engine.get("Version") != "29.8.0"
        or not engine.get("GitCommit", "").startswith("3ce5872")
        or engine.get("Os") != "linux"
        or engine.get("Arch") != "amd64"
    ):
        raise ValueError("browser_profile_engine_not_qualified")
    source = Path(__file__).resolve().parent / PROFILE_NAME
    profile_metadata(source)
    target = output / PROFILE_NAME
    if target.exists() or target.is_symlink():
        profile_metadata(target)
    else:
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
    target.chmod(0o400)
    return {
        **profile_metadata(target),
        "docker_version": engine["Version"],
        "docker_git_commit": engine["GitCommit"],
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    argv: list[str], *, timeout: int = 600, env: dict[str, str] | None = None
) -> str:
    return subprocess.run(
        argv,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        env=env,
    ).stdout


def load_lock(path: Path) -> dict:
    lock = json.loads(path.read_text())
    if lock.get("schema_version") != 1 or not IMAGE.fullmatch(
        lock.get("base_image", "")
    ):
        raise ValueError("invalid_base_pin")
    stamp = lock.get("snapshot", "")
    if (
        not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp)
        or lock.get("suite") != "bookworm"
        or lock.get("platform") != "linux/amd64"
    ):
        raise ValueError("invalid_snapshot_or_platform")
    expected = [
        f"https://snapshot.debian.org/archive/{archive}/{stamp}/"
        for archive in ("debian", "debian-security")
    ]
    if lock.get("repositories") != expected or lock.get("apt_packages") != [
        "chromium",
        "chromium-driver",
        "fonts-liberation",
    ]:
        raise ValueError("invalid_repository_or_package_scope")
    return lock


def install_image() -> None:
    root = Path("/opt/swarmer")
    lock = load_lock(root / "browser-system.lock.json")
    # BuildKit does not create /.dockerenv. Require the deliberate Dockerfile
    # invocation and exact image path/distribution instead of guessing that marker.
    if (
        os.getuid() != 0
        or os.environ.get("SWARMER_BROWSER_IMAGE_BUILD") != "1"
        or Path(__file__).resolve() != root / "setup_browser_runtime.py"
        or not Path("/etc/debian_version").read_text().startswith("12.")
    ):
        raise RuntimeError("installation_requires_candidate_container")
    os.umask(0o022)  # Image artifacts must be readable by the nonroot runtime.
    # Signed immutable snapshot fixes the complete apt resolution, not just top-level names.
    sources = Path("/etc/apt/sources.list.d")
    for path in sources.iterdir():
        if path.suffix in {".list", ".sources"}:
            path.unlink()
    Path("/etc/apt/sources.list").write_text("")
    sources.joinpath("swarmer-browser.list").write_text(
        "\n".join(
            f"deb [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] {url} {suite} main"
            for url, suite in zip(
                lock["repositories"], ("bookworm", "bookworm-security"), strict=True
            )
        )
        + "\n"
    )
    environment = dict(
        os.environ,
        DEBIAN_FRONTEND="noninteractive",
        PLAYWRIGHT_BROWSERS_PATH=str(root / "playwright-browsers"),
    )
    print(
        run(["apt-get", "-o", "Acquire::Retries=0", "update"], env=environment),
        flush=True,
    )
    print(
        run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *lock["apt_packages"],
            ],
            env=environment,
        ),
        flush=True,
    )
    browser_version = run(["dpkg-query", "-W", "-f=${Version}", "chromium"]).strip()
    driver_version = run(
        ["dpkg-query", "-W", "-f=${Version}", "chromium-driver"]
    ).strip()
    if browser_version != driver_version:
        raise RuntimeError("chromium_driver_version_mismatch")
    print(
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(root / "runtime-tools.lock"),
                "-r",
                str(root / "runtime-browser.lock"),
            ],
            env=environment,
        ),
        flush=True,
    )
    print(run([sys.executable, "-m", "pip", "check"], env=environment), flush=True)
    print(
        run(
            [sys.executable, "-m", "playwright", "install", "chromium"], env=environment
        ),
        flush=True,
    )
    installed = {
        d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
    }
    playwright_root = Path(
        importlib.metadata.distribution("playwright").locate_file("playwright")
    )
    registry = playwright_root / "driver/package/browsers.json"
    binaries = [Path("/usr/lib/chromium/chromium"), Path("/usr/bin/chromedriver")]
    binaries.extend(
        p
        for p in (root / "playwright-browsers").rglob("*")
        if p.is_file()
        and p.name
        in {"chrome", "headless_shell", "chrome-headless-shell", "ffmpeg-linux"}
    )
    inventory = {
        "schema_version": 1,
        "system_lock": lock,
        "sandbox_profile": profile_metadata(root / PROFILE_NAME),
        "python_distributions": installed,
        "apt_packages": run(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n"]
        ).splitlines(),
        "chromium_package_version": browser_version,
        "chromedriver_package_version": driver_version,
        "playwright_browser_registry": json.loads(registry.read_text()),
        "playwright_registry_sha256": digest(registry),
        "binary_sha256": {str(p): digest(p) for p in binaries},
        "input_sha256": {
            name: digest(root / name) for name in FILES if (root / name).is_file()
        },
        "apt_index_sha256": {
            p.name: digest(p)
            for p in Path("/var/lib/apt/lists").iterdir()
            if p.is_file()
        },
    }
    (root / "browser-inventory.json").write_text(
        json.dumps(inventory, sort_keys=True, indent=2) + "\n"
    )
    # Existing tools and all newly added trusted code/artifacts become immutable in the image.
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(
                0o555 if path.is_dir() else (path.stat().st_mode & 0o111) | 0o444
            )
    root.chmod(0o555)


def probe_command(
    image: str,
    name: str,
    engine: str,
    uid: int,
    gid: int,
    *,
    profile: Path | None = None,
) -> list[str]:
    if (
        not IMAGE.fullmatch(image)
        or engine not in {"selenium", "playwright"}
        or uid <= 0
        or gid < 0
    ):
        raise ValueError("invalid_probe_arguments")
    if not re.fullmatch(
        r"swarmer-browser-proof-[a-f0-9]{32}-(selenium|playwright)", name
    ):
        raise ValueError("invalid_owned_container_name")
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "2",
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        "fsize=268435456:268435456",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=256m,mode=1777",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env",
        "HOME=/tmp",
        image,
        "python",
        "-I",
        "/opt/swarmer/browser_runtime_probe.py",
        engine,
    ]
    if engine == "playwright":
        command[command.index(image) : command.index(image)] = [
            "--env",
            "DEBUG=pw:browser",
        ]
    if profile is not None:
        metadata = profile_metadata(profile)
        command[command.index("--pids-limit") + 1] = "256"
        command[command.index(image) : command.index(image)] = [
            "--security-opt",
            "seccomp=" + metadata["path"],
        ]
    return command


def validate_pass_receipt(
    receipt: dict, engine: str, uid: int, *, pids_limit: int = 64
) -> None:
    if receipt.get("status") != "passed":
        return
    sandbox = receipt.get("sandbox", {})
    required = {
        "fresh_storage",
        "page_title",
        "click_dom_update",
        "local_storage_survives_reload",
    }
    if (
        receipt.get("schema_version") != 1
        or receipt.get("engine") != engine
        or receipt.get("fresh_session") is not True
        or receipt.get("sandbox_disabled") is not False
        or set(receipt.get("assertions", [])) != required
        or sandbox.get("uid") != uid
        or any(
            sandbox.get(key) is not True
            for key in (
                "no_new_privileges",
                "effective_capabilities_zero",
                "root_read_only",
            )
        )
        or sandbox.get("network_interfaces") != ["lo"]
        or sandbox.get("non_loopback_ipv4_routes") != 0
        or sandbox.get("apparmor_profile") != "docker-default (enforce)"
        or sandbox.get("seccomp_mode") != 2
        or sandbox.get("pids_limit") != pids_limit
        or not isinstance(receipt.get("browser_version"), str)
        or not receipt["browser_version"]
    ):
        raise ValueError("unproven_success_receipt")


def qualify(image: str, output: Path, *, browser_profile: bool = False) -> dict:
    profile = prepare_browser_profile(output) if browser_profile else None
    receipts = []
    for engine in ("selenium", "playwright"):
        name = "swarmer-browser-proof-" + uuid.uuid4().hex + "-" + engine
        command = probe_command(
            image,
            name,
            engine,
            os.getuid(),
            os.getgid(),
            profile=Path(profile["path"]) if profile else None,
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=45, check=False
            )
            lines = result.stdout.splitlines()
            receipt = (
                json.loads(lines[-1])
                if lines
                else {"status": "failed", "failure_code": "probe_no_receipt"}
            )
            validate_pass_receipt(
                receipt, engine, os.getuid(), pids_limit=256 if profile else 64
            )
            native_logs = receipt.pop("_private_native_logs", None)
            if isinstance(native_logs, str):
                native_path = output / (engine + "-native.log")
                native_path.write_text(native_logs[-24000:])
                native_path.chmod(0o600)
                receipt["native_log_artifact"] = native_path.name
                receipt["native_log_sha256"] = digest(native_path)
            receipt.update(exit_code=result.returncode, command=command)
            if getattr(result, "stderr", ""):
                transport = output / (engine + "-transport.log")
                transport.write_text(result.stderr[-24000:])
                transport.chmod(0o600)
                receipt.update(
                    transport_log_artifact=transport.name,
                    transport_log_sha256=digest(transport),
                )
            if result.returncode != 0:
                receipt["status"] = "failed"
        except (ValueError, TypeError, AttributeError):
            receipt = {
                "engine": engine,
                "status": "failed",
                "failure_code": "probe_invalid_receipt",
                "command": command,
            }
        except subprocess.TimeoutExpired as exc:
            receipt = {
                "engine": engine,
                "status": "failed",
                "failure_code": "probe_outer_timeout",
                "command": command,
            }
            if exc.stderr:
                transport = output / (engine + "-transport.log")
                value = (
                    exc.stderr.decode(errors="replace")
                    if isinstance(exc.stderr, bytes)
                    else exc.stderr
                )
                transport.write_text(value[-24000:])
                transport.chmod(0o600)
                receipt.update(
                    transport_log_artifact=transport.name,
                    transport_log_sha256=digest(transport),
                )
        finally:
            # Only our unique candidate container may survive a Docker CLI timeout.
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=10,
                check=False,
            )
        receipt["outer_elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["sandbox_profile"] = profile
        receipts.append(receipt)
    result = {
        "image_id": image,
        "sandbox_profile": profile,
        "qualification_source_sha256": {
            name: digest(Path(__file__).resolve().parent / name)
            for name in ("setup_browser_runtime.py", "browser_runtime_probe.py")
        },
        "status": "passed"
        if all(r["status"] == "passed" for r in receipts)
        else "blocked",
        "probes": receipts,
    }
    (output / "qualification.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def build(output: Path, *, browser_profile: bool = False) -> dict:
    if os.getuid() == 0:
        raise RuntimeError("builder_requires_operator_account")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    source = Path(__file__).resolve().parent
    lock = load_lock(source / "browser-system.lock.json")
    for name in FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing_or_symlinked_build_input")
        shutil.copyfile(path, output / name)
    inputs = {name: digest(output / name) for name in FILES}
    base = json.loads(run(["docker", "image", "inspect", lock["base_image"]]))[0]
    if (
        base["Id"] != lock["base_image"]
        or base["Os"] + "/" + base["Architecture"] != lock["platform"]
    ):
        raise ValueError("base_image_identity_mismatch")
    suffix = uuid.uuid4().hex
    base_tag = "swarmer-browser-base-candidate:" + suffix
    candidate_tag = "swarmer-browser-candidate:" + suffix
    run(["docker", "tag", lock["base_image"], base_tag], timeout=10)
    command = [
        "docker",
        "build",
        "--pull=false",
        "--platform",
        lock["platform"],
        "--build-arg",
        "BASE_IMAGE=" + base_tag,
        "--file",
        str(output / "Dockerfile.browser"),
        "--iidfile",
        str(output / "image-id"),
        "--tag",
        candidate_tag,
        str(output),
    ]
    (output / "build-inputs.json").write_text(
        json.dumps(
            {"base_image": base["Id"], "input_sha256": inputs, "command": command},
            indent=2,
        )
        + "\n"
    )
    with (output / "build.log").open("w") as log:
        subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT, timeout=1200, check=True
        )
    image = (output / "image-id").read_text().strip()
    if not IMAGE.fullmatch(image):
        raise ValueError("invalid_built_image_identity")
    (output / "image-inspect.json").write_text(
        run(["docker", "image", "inspect", image])
    )
    inventory = run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65532:65532",
            image,
            "python",
            "-I",
            "-c",
            "print(open('/opt/swarmer/browser-inventory.json').read())",
        ],
        timeout=30,
    )
    (output / "image-inventory.json").write_text(inventory)
    if inputs != {name: digest(output / name) for name in FILES}:
        raise RuntimeError("build_input_changed")
    return qualify(image, output, browser_profile=browser_profile)


def overlay(image: str, output: Path, *, browser_profile: bool = False) -> dict:
    """Refresh only trusted Python/catalogue bytes; immutable dependencies stay fixed."""
    if os.getuid() == 0 or not IMAGE.fullmatch(image):
        raise ValueError("invalid_overlay_identity")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    source = Path(__file__).resolve().parent
    original = json.loads(
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "0:0",
                image,
                "python",
                "-I",
                "-c",
                "print(open('/opt/swarmer/browser-inventory.json').read())",
            ],
            timeout=30,
        )
    )
    for name in (
        "browser-system.lock.json",
        "runtime-tools.lock",
        "runtime-browser.lock",
    ):
        if original["input_sha256"].get(name) != digest(source / name):
            raise ValueError("overlay_dependency_changed_requires_full_build")
    copied = (
        "check_harness.py",
        "dependency-catalog.json",
        "setup_browser_runtime.py",
        "browser_runtime_probe.py",
        PROFILE_NAME,
        "browser-seccomp-v1.provenance.json",
        "browser-seccomp-LICENSE",
    )
    for name in copied:
        if (source / name).is_symlink():
            raise ValueError("overlay_symlink_input")
        shutil.copyfile(source / name, output / name)
    inputs = {name: digest(output / name) for name in copied}
    suffix = uuid.uuid4().hex
    base_tag = "swarmer-browser-overlay-parent:" + suffix
    run(["docker", "tag", image, base_tag], timeout=10)
    script = (
        "import hashlib,json,pathlib; p=pathlib.Path('/opt/swarmer'); f=p/'browser-inventory.json'; "
        "v=json.loads(f.read_text()); v['overlay_parent_image']=" + repr(image) + "; "
        "v['input_sha256'].update({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in "
        + repr(copied)
        + "}); "
        "v['sandbox_profile']="
        + repr(
            profile_metadata(source / PROFILE_NAME)
            | {"path": "/opt/swarmer/" + PROFILE_NAME}
        )
        + "; "
        "f.chmod(0o600); f.write_text(json.dumps(v,sort_keys=True,indent=2)+chr(10)); f.chmod(0o444); "
        "[(p/n).chmod(0o444) for n in " + repr(copied) + "]; "
        "roots=[p,pathlib.Path('/usr/local/lib/python3.12/site-packages'),pathlib.Path('/usr/local/bin')]; "
        "[(x.chmod(0o555 if x.is_dir() else (x.stat().st_mode&0o111)|0o444)) for r in roots for x in [r,*r.rglob('*')] if not x.is_symlink()]"
    )
    import shlex

    dockerfile = (
        "FROM "
        + base_tag
        + "\nUSER 0:0\nCOPY "
        + " ".join(copied)
        + " /opt/swarmer/\nRUN python -I -c "
        + shlex.quote(script)
        + "\nUSER 65532:65532\n"
    )
    (output / "Dockerfile.overlay").write_text(dockerfile)
    command = [
        "docker",
        "build",
        "--pull=false",
        "--network=none",
        "--file",
        str(output / "Dockerfile.overlay"),
        "--iidfile",
        str(output / "image-id"),
        "--tag",
        "swarmer-browser-candidate:" + suffix,
        str(output),
    ]
    (output / "build-inputs.json").write_text(
        json.dumps(
            {"parent_image": image, "input_sha256": inputs, "command": command},
            indent=2,
        )
        + "\n"
    )
    with (output / "build.log").open("w") as log:
        subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True
        )
    final = (output / "image-id").read_text().strip()
    if not IMAGE.fullmatch(final):
        raise ValueError("invalid_overlay_image")
    (output / "image-inspect.json").write_text(
        run(["docker", "image", "inspect", final])
    )
    (output / "image-inventory.json").write_text(
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65532:65532",
                final,
                "python",
                "-I",
                "-c",
                "print(open('/opt/swarmer/browser-inventory.json').read())",
            ],
            timeout=30,
        )
    )
    final_inventory = json.loads((output / "image-inventory.json").read_text())
    if any(
        final_inventory["input_sha256"].get(name) != value
        for name, value in inputs.items()
    ):
        raise RuntimeError("overlay_source_digest_mismatch")
    if any(
        final_inventory[key] != original[key]
        for key in (
            "apt_packages",
            "python_distributions",
            "binary_sha256",
            "playwright_registry_sha256",
        )
    ):
        raise RuntimeError("overlay_changed_dependency_inventory")
    return qualify(final, output, browser_profile=browser_profile)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("install-image", "build", "qualify", "overlay")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image")
    parser.add_argument(
        "--browser-profile",
        action="store_true",
        help="Opt in to the qualified Docker29.8.0 amd64 browser seccomp profile and pids256.",
    )
    args = parser.parse_args()
    if args.mode == "install-image":
        install_image()
        return 0
    if args.output is None:
        parser.error("--output is required")
    if args.mode == "build":
        result = build(args.output, browser_profile=args.browser_profile)
    elif args.mode == "overlay":
        result = overlay(
            args.image or "", args.output, browser_profile=args.browser_profile
        )
    else:
        result = qualify(
            args.image or "", args.output, browser_profile=args.browser_profile
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
