#!/usr/bin/env python3
"""Private, short-lived HTTPS client for the DEBUG-only iPhone application API."""

import argparse
import base64
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TLS_NAME = "mongars-automation.local"
PREFIX = "DEVICECTL_CHILD_SWARMER_AUTOMATION_"
MAX_RESPONSE = 1_048_576
JOB_ID = re.compile(r"job_[a-z0-9]+_[1-9][0-9]*\Z")
INSTANCE = re.compile(r"[a-z0-9]{1,128}\Z")
KEY = re.compile(r"[A-Za-z0-9_.:-]{16,128}\Z")


class ClientError(Exception):
    """Only fixed, non-sensitive diagnostics reach the terminal."""


def reject_constant(_):
    raise ValueError("Non-finite JSON number")


def literal_host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ClientError("--host must be an explicit IPv4 or IPv6 address.") from None
    if address.is_unspecified or address.is_multicast:
        raise ClientError("An unspecified or multicast host is not supported.")
    return str(address)


def child_environment():
    # Do not accidentally forward unrelated environment into the application.
    return {k: v for k, v in os.environ.items() if not k.startswith("DEVICECTL_CHILD_")}


def run_tool(argv, *, env=None, input_bytes=None, timeout=35):
    try:
        result = subprocess.run(
            argv,
            input=input_bytes,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError(
            "Local tool failed or timed out; raw output suppressed."
        ) from None
    if result.returncode:
        raise ClientError("Local tool failed; raw output suppressed.")
    return result.stdout


def read_device(device, directory, *, details=False, timeout_seconds=30):
    output = directory / "device.json"
    run_tool(
        [
            "xcrun",
            "devicectl",
            "device",
            "info",
            "details",
            "--device",
            device,
            "--json-output",
            str(output),
            "--timeout",
            str(timeout_seconds),
        ],
        env=child_environment(),
        timeout=timeout_seconds + 1,
    )
    try:
        result = json.loads(output.read_text())["result"]
        hardware = result["hardwareProperties"]
        connection = result["connectionProperties"]
        properties = result["deviceProperties"]
        identifiers = {str(result["identifier"]).lower(), str(hardware["udid"]).lower()}
        ready = (
            device.lower() in identifiers
            and hardware.get("platform") == "iOS"
            and hardware.get("reality") == "physical"
            and connection.get("pairingState") == "paired"
            and connection.get("tunnelState") == "connected"
            and properties.get("bootState") == "booted"
            and properties.get("ddiServicesAvailable") is True
        )
        if not ready:
            raise ValueError
        if details:
            return {
                "identifier": result["identifier"],
                "udid": hardware["udid"],
                "tunnelAddress": literal_host(connection["tunnelIPAddress"]),
            }
        return result["identifier"]
    except (ValueError, KeyError, TypeError, OSError):
        raise ClientError(
            "Fresh device identity/readiness was not confirmed; no launch."
        ) from None


def certificates(directory):
    """Keys/P12 exist only inside the private temporary directory until launch ends."""
    password = secrets.token_hex(32)
    ca_key, ca, key, csr, cert, p12 = (
        directory / name
        for name in (
            "ca.key",
            "ca.pem",
            "server.key",
            "server.csr",
            "server.pem",
            "server.p12",
        )
    )
    extensions = directory / "server.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:" + TLS_NAME + "\n"
    )
    ca_config = directory / "ca.cnf"
    ca_config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n"
        "[dn]\nCN=monGARS temporary automation CA\n[ca]\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
    )
    commands = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "1",
            "-config",
            str(ca_config),
            "-keyout",
            str(ca_key),
            "-out",
            str(ca),
        ],
        [
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=" + TLS_NAME,
            "-keyout",
            str(key),
            "-out",
            str(csr),
        ],
        [
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca),
            "-CAkey",
            str(ca_key),
            "-set_serial",
            str(secrets.randbits(128) or 1),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
            "-out",
            str(cert),
        ],
    ]
    for command in commands:
        run_tool(["openssl", *command])
    # Apple's SecPKCS12Import supports these P12 algorithms; TLS uses SHA256/RSA.
    run_tool(
        [
            "openssl",
            "pkcs12",
            "-export",
            "-in",
            str(cert),
            "-inkey",
            str(key),
            "-certfile",
            str(ca),
            "-out",
            str(p12),
            "-passout",
            "stdin",
            "-keypbe",
            "PBE-SHA1-3DES",
            "-certpbe",
            "PBE-SHA1-3DES",
            "-macalg",
            "sha1",
        ],
        input_bytes=(password + "\n").encode(),
    )
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    encoded = base64.b64encode(p12.read_bytes()).decode("ascii")
    if len(encoded) > 65_536:
        raise ClientError("Generated identity exceeds the native limit.")
    return ca.read_text(), hashlib.sha256(der).hexdigest(), encoded, password


def save_session(path, session):
    # Exclusive creation: never overwrite a live session or follow a symlink.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(session, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ClientError(
            "Session creation failed; choose a new private session path."
        ) from None


def load_session(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError
            if info.st_size > 65_536:
                raise ValueError
            session = json.load(stream)
        token = session["token"]
        created, expiry = session["createdAt"], session["expiresAt"]
        valid = (
            session["version"] == 1
            and session["tlsName"] == TLS_NAME
            and isinstance(token, str)
            and 32 <= len(token) <= 256
            and all(33 <= ord(c) <= 126 for c in token)
            and isinstance(created, (int, float))
            and math.isfinite(created)
            and isinstance(expiry, (int, float))
            and math.isfinite(expiry)
            and 0 < expiry - created <= 3600
            and created <= time.time() < expiry
            and isinstance(session["port"], int)
            and 1024 <= session["port"] <= 65535
            and re.fullmatch(r"[a-f0-9]{64}", session["certificateSha256"])
            and isinstance(session["caPem"], str)
            and (
                session.get("instanceId") is None
                or (
                    isinstance(session["instanceId"], str)
                    and INSTANCE.fullmatch(session["instanceId"])
                )
            )
        )
        if not valid:
            raise ValueError
        session["host"] = literal_host(session["host"])
        return session
    except (OSError, ValueError, KeyError, TypeError):
        raise ClientError(
            "Session must be owned by you, mode 600, valid and unexpired."
        ) from None


def bind_instance(path, session, response):
    """Bind once under a file lock. A torn write fails closed; never adopt a new runtime."""
    instance = response.get("instanceId")
    if (
        response.get("state") != "ready"
        or not isinstance(instance, str)
        or not INSTANCE.fullmatch(instance)
    ):
        raise ClientError("Health did not confirm a ready application instance.")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError
            stored = json.load(stream)
            # Concurrent first-health binding is the only permitted difference.
            if {k: v for k, v in stored.items() if k != "instanceId"} != {
                k: v for k, v in session.items() if k != "instanceId"
            }:
                raise ValueError
            if stored.get("instanceId") not in (None, instance):
                raise ClientError(
                    "Application instance changed; session is fenced. Do not resend old commands."
                )
            if stored.get("instanceId") is None:
                stored["instanceId"] = instance
                stream.seek(0)
                json.dump(stored, stream)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            session["instanceId"] = instance
    except (OSError, ValueError, TypeError):
        raise ClientError("Session binding failed; no command sent.") from None


def refresh_launch_route(path, session, initial, fresh, monitor=None):
    """Refresh only the selected device's known tunnel before binding any API instance."""
    if any(
        initial[key].lower() != fresh[key].lower() for key in ("identifier", "udid")
    ):
        raise ClientError(
            "Device identity changed after launch; no HTTPS request sent."
        )
    updated = dict(session)
    if session.get("deviceTunnel") is True:
        updated["host"] = fresh["tunnelAddress"]
    if monitor is not None:
        updated["consoleMonitor"] = {
            "pid": monitor.pid,
            "ownerUid": os.getuid(),
            "startedAt": time.time(),
            "lifetime": "foreground_session",
        }
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError
            stored = json.load(stream)
            if stored != session or stored.get("instanceId") is not None:
                raise ValueError
            stream.seek(0)
            json.dump(updated, stream)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        session.clear()
        session.update(updated)
    except (OSError, ValueError, TypeError):
        raise ClientError(
            "Session route changed concurrently; no HTTPS request sent."
        ) from None


def close_console(monitor):
    """Never send a catchable signal that devicectl would forward to the app."""
    try:
        if monitor.poll() is None:
            # This direct child is still ours (unreaped); no stored-PID lookup or process group kill.
            monitor.kill()  # SIGKILL of the Mac child only, never devicectl process terminate.
        monitor.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError(
            "Local console monitor cleanup failed; no app signal requested."
        ) from None


def hold_console(monitor, session, path, deadline):
    while monitor.poll() is None:
        remaining = min(deadline - time.monotonic(), session["expiresAt"] - time.time())
        if remaining <= 0:
            return {
                "event": "console_closed",
                "reason": "session_expired",
                "session": str(path),
            }
        try:
            monitor.wait(timeout=min(1, remaining))
        except subprocess.TimeoutExpired:
            pass
    if monitor.returncode != 0:
        raise ClientError(
            "Console monitor exited unsuccessfully; no automatic relaunch."
        )
    return {"event": "console_closed", "reason": "monitor_exited", "session": str(path)}


def launch(args):
    host = None if args.device_tunnel else literal_host(args.host)
    if not 1 <= args.ttl <= 3600 or not 1024 <= args.port <= 65535:
        raise ClientError("TTL must be 1–3600 seconds and port 1024–65535.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.bundle_id):
        raise ClientError("Invalid bundle identifier.")
    if args.session:
        session_path = Path(args.session).absolute()
        if session_path.exists() or session_path.is_symlink():
            raise ClientError("Session already exists; choose a new path.")
    else:
        session_path = (
            Path(tempfile.mkdtemp(prefix="swarmer-iphone-session-")) / "session.json"
        )
    old_umask = os.umask(0o077)
    monitor = None
    console_directory = None
    old_handlers = {}
    if args.console:

        def interrupted(_signal, _frame):
            raise KeyboardInterrupt

        for name in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[name] = signal.signal(name, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="swarmer-iphone-tls-") as temporary:
            directory = Path(temporary)
            initial = read_device(args.device, directory, details=True)
            device = initial["identifier"]
            ca, pin, p12, password = certificates(directory)
            now = time.time()
            console_deadline = time.monotonic() + args.ttl
            session = {
                "version": 1,
                "device": device,
                "bundleId": args.bundle_id,
                "host": initial["tunnelAddress"] if args.device_tunnel else host,
                "deviceTunnel": args.device_tunnel,
                "port": args.port,
                "tlsName": TLS_NAME,
                "caPem": ca,
                "certificateSha256": pin,
                "token": secrets.token_urlsafe(48),
                "createdAt": now,
                "expiresAt": now + args.ttl,
            }
            save_session(session_path, session)
            values = {
                "ENABLE": "1",
                "PORT": str(args.port),
                "TOKEN": session["token"],
                "TLS_P12": p12,
                "TLS_PASSWORD": password,
                "TTL_SECONDS": str(args.ttl),
            }
            environment = child_environment()
            environment.update({PREFIX + key: value for key, value in values.items()})
            launch_output = directory / "launch.json"
            if args.console:
                console_directory = tempfile.TemporaryDirectory(
                    prefix="swarmer-iphone-console-"
                )
                launch_output = Path(console_directory.name) / "launch.json"
            argv = [
                "xcrun",
                "devicectl",
                "device",
                "process",
                "launch",
                "--device",
                device,
                "--terminate-existing",
                "--json-output",
                str(launch_output),
            ]
            argv += ["--console"] if args.console else ["--timeout", "30"]
            argv.append(args.bundle_id)
            try:
                if args.console:
                    monitor = subprocess.Popen(
                        argv,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                else:
                    run_tool(argv, env=environment)
            except (ClientError, OSError):
                raise ClientError(
                    "Launch outcome is uncertain; no retry. Session retained at "
                    + str(session_path)
                ) from None
            finally:
                environment.clear()
                values.clear()
                p12 = password = None
            # CoreDevice can change its tunnel while launching. Never parse its console text.
            fresh = read_device(device, directory, details=True, timeout_seconds=5)
            refresh_launch_route(session_path, session, initial, fresh, monitor)
        # The private key/P12 directory is now removed, even while the console stays alive.
        deadline = min(time.monotonic() + 30, console_deadline)
        while time.monotonic() < deadline:
            if monitor is not None and monitor.poll() is not None:
                raise ClientError(
                    "Console exited before HTTPS readiness; no automatic relaunch."
                )
            try:
                status, health = request(session, "GET", "/v1/health", timeout=2)
            except ClientError:
                status, health = None, None
            if status == 200:
                bind_instance(session_path, session, health)
                ready = {
                    "launched": True,
                    "apiVerified": True,
                    "session": str(session_path),
                    "expiresAt": session["expiresAt"],
                    "host": session["host"],
                    "health": health,
                }
                if monitor is None:
                    return ready
                print(
                    json.dumps(
                        dict(ready, event="ready", consoleHeld=True), ensure_ascii=False
                    ),
                    flush=True,
                )
                return hold_console(monitor, session, session_path, console_deadline)
            if args.device_tunnel and deadline - time.monotonic() > 1:
                # Launch can acquire its assertion after the first details read. Only GET
                # readiness may rediscover an unbound route; never relaunch or resend a POST.
                with tempfile.TemporaryDirectory(
                    prefix="swarmer-iphone-discovery-"
                ) as fresh_dir:
                    try:
                        fresh = read_device(
                            device,
                            Path(fresh_dir),
                            details=True,
                            timeout_seconds=min(
                                5, max(1, int(deadline - time.monotonic()) - 1)
                            ),
                        )
                    except ClientError:
                        fresh = None
                    if fresh is not None:
                        refresh_launch_route(session_path, session, initial, fresh)
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise ClientError(
            "Launched but HTTPS readiness was not confirmed. Session retained at "
            + str(session_path)
        )
    finally:
        try:
            if monitor is not None:
                close_console(monitor)
        finally:
            if console_directory is not None:
                console_directory.cleanup()
            for name, handler in old_handlers.items():
                signal.signal(name, handler)
            os.umask(old_umask)


def pinned_socket(session, timeout):
    context = ssl.create_default_context(cadata=session["caPem"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    host = literal_host(session["host"])
    address = ipaddress.ip_address(host)
    raw = socket.socket(
        socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM
    )
    secure = None
    try:
        raw.settimeout(timeout)
        if address.version == 6:
            scope = getattr(address, "scope_id", None)
            scope_id = (
                int(scope)
                if scope and scope.isdigit()
                else socket.if_nametoindex(scope)
                if scope
                else 0
            )
            raw.connect((host.split("%", 1)[0], session["port"], 0, scope_id))
        else:
            raw.connect((host, session["port"]))
        secure = context.wrap_socket(raw, server_hostname=TLS_NAME)
        # Python 3.9/LibreSSL cannot disable CN fallback through the context property.
        # Require the exact DNS SAN explicitly, in addition to TLS hostname validation.
        if ("DNS", TLS_NAME) not in secure.getpeercert().get("subjectAltName", ()):
            raise ClientError("Certificate SAN mismatch; no authorization sent.")
        actual = hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, session["certificateSha256"]):
            raise ClientError("Certificate pin mismatch; no authorization sent.")
        return secure
    except BaseException:
        (secure or raw).close()
        raise


def request(session, method, path, body=None, timeout=15):
    if not session["createdAt"] <= time.time() < session["expiresAt"]:
        raise ClientError("Session expired; no request sent.")
    payload = (
        json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        if body is not None
        else b""
    )
    if len(payload) > 65_536:
        raise ClientError("Request exceeds the native body limit.")
    sent = False
    connection = http.client.HTTPConnection(TLS_NAME, session["port"])
    try:
        # Connect and authenticate the TLS peer before even constructing Authorization.
        connection.sock = pinned_socket(
            session, min(timeout, session["expiresAt"] - time.time())
        )
        if time.time() >= session["expiresAt"]:
            raise ClientError(
                "Session expired during TLS connection; no authorization sent."
            )
        sent = True
        connection.request(
            method,
            path,
            body=payload,
            headers={
                "Authorization": "Bearer " + session["token"],
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("apiVersion") != "1":
            raise ValueError
        if (
            200 <= response.status < 300
            and session.get("instanceId") is not None
            and value.get("instanceId") != session["instanceId"]
        ):
            raise ClientError(
                "Application instance changed; session is fenced. Do not resend old commands."
            )
        return response.status, value
    except (OSError, ValueError, http.client.HTTPException):
        if method == "POST" and sent:
            raise ClientError(
                "Command outcome unknown; no retry. Recover in this same session with idempotency key "
                + body["idempotencyKey"]
            ) from None
        raise ClientError(
            "HTTPS request failed; raw response suppressed. No retry."
        ) from None
    finally:
        connection.close()


def checked_job(response):
    job = response.get("job")
    if (
        not isinstance(job, dict)
        or not isinstance(job.get("id"), str)
        or not JOB_ID.fullmatch(job["id"])
    ):
        raise ClientError("Invalid job response; no command retry.")
    if job.get("state") not in {"running", "succeeded", "failed", "uncertain"}:
        raise ClientError("Unknown job state; no command retry.")
    if not isinstance(response.get("instanceId"), str) or not job["id"].startswith(
        "job_" + response["instanceId"] + "_"
    ):
        raise ClientError("Job instance mismatch; no command retry.")
    return job


def execute(args):
    if args.action == "launch":
        return launch(args), 0
    session = load_session(args.session)
    if args.action != "health" and session.get("instanceId") is None:
        raise ClientError("Session is not bound; verify health before any command.")
    if args.action in {"health", "catalog", "job"}:
        if args.action == "job" and not JOB_ID.fullmatch(args.job_id):
            raise ClientError("Invalid job ID.")
        if args.action == "job" and not args.job_id.startswith(
            "job_" + session["instanceId"] + "_"
        ):
            raise ClientError("Job belongs to another application instance.")
        path = (
            "/v1/jobs/" + args.job_id if args.action == "job" else "/v1/" + args.action
        )
        status, response = request(session, "GET", path)
        if args.action == "health" and status == 200:
            bind_instance(args.session, session, response)
        failed = (
            args.action == "job"
            and status == 200
            and checked_job(response)["state"] in {"failed", "uncertain"}
        )
        return response, 0 if status == 200 and not failed else 1
    key = args.idempotency_key or str(uuid.uuid4())
    if not KEY.fullmatch(key):
        raise ClientError(
            "Idempotency key must contain 16–128 letters, digits or _ . : -."
        )
    if not re.fullmatch(r"[a-z][a-zA-Z0-9_.]{0,95}", args.command):
        raise ClientError("Invalid command name.")
    try:
        if args.input_file == "-":
            raw = sys.stdin.read(65_537)
        elif args.input_file:
            with open(args.input_file) as stream:
                raw = stream.read(65_537)
        else:
            raw = "{}"
        value = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise TypeError
        # Validate serialization and bounds before attempting any network operation.
        json.dumps(value, allow_nan=False)
        if len(raw) > 65_536:
            raise ValueError
    except (OSError, ValueError, TypeError, RecursionError):
        raise ClientError(
            "Command input must be a JSON object; content suppressed."
        ) from None
    try:
        status, response = request(
            session,
            "POST",
            "/v1/commands",
            {
                "command": args.command,
                "input": value,
                "idempotencyKey": key,
                "instanceId": session["instanceId"],
            },
        )
        if status not in {200, 202}:
            return response, 1
        job = checked_job(response)
        deadline = min(
            time.monotonic() + args.wait_timeout,
            time.monotonic() + session["expiresAt"] - time.time(),
        )
        while args.wait and job["state"] == "running":
            if time.monotonic() + args.poll_interval >= deadline:
                return response, 2
            time.sleep(args.poll_interval)
            status, response = request(session, "GET", "/v1/jobs/" + job["id"])
            if status != 200:
                return response, 1
            next_job = checked_job(response)
            if next_job["id"] != job["id"]:
                raise ClientError("Job identity changed; polling stopped.")
            job = next_job
        return response, 1 if job["state"] in {"failed", "uncertain"} else 0
    except (ClientError, KeyboardInterrupt) as error:
        raise ClientError(
            (str(error) if isinstance(error, ClientError) else "Interrupted.")
            + " No automatic resend; same-session recovery key: "
            + key
        ) from None


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    for name in ("launch", "health", "catalog", "call", "job"):
        command = commands.add_parser(name)
        command.add_argument(
            "--session",
            required=name != "launch",
            help="Private session JSON (mode 600)",
        )
        if name == "launch":
            command.add_argument(
                "--device", required=True, help="Exact UDID or CoreDevice UUID"
            )
            address = command.add_mutually_exclusive_group(required=True)
            address.add_argument(
                "--host", help="Explicit IPv4/IPv6 address; never changed"
            )
            address.add_argument(
                "--device-tunnel",
                action="store_true",
                help="Resolve this exact device's fresh CoreDevice tunnel during launch readiness",
            )
            command.add_argument("--bundle-id", default="org.27pm.mongars")
            command.add_argument("--port", type=int, default=8766)
            command.add_argument("--ttl", type=int, default=900)
            command.add_argument(
                "--console",
                action="store_true",
                help="Hold the launch console in this terminal until app exit or session expiry",
            )
        elif name == "call":
            command.add_argument("command")
            command.add_argument(
                "--input-file", help="JSON object file, or - for stdin; default {}"
            )
            command.add_argument("--idempotency-key")
            command.add_argument("--wait", action="store_true")
            command.add_argument("--wait-timeout", type=float, default=120)
            command.add_argument("--poll-interval", type=float, default=1)
        elif name == "job":
            command.add_argument("job_id")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "call" and (
            not 0.1 <= args.poll_interval <= 30 or not 0 < args.wait_timeout <= 3600
        ):
            raise ClientError(
                "Poll interval must be 0.1–30 seconds; wait timeout 1–3600 seconds."
            )
        response, code = execute(args)
        print(json.dumps(response, ensure_ascii=False))
        return code
    except (ClientError, KeyboardInterrupt) as error:
        message = (
            str(error)
            if isinstance(error, ClientError)
            else "Interrupted; no automatic retry."
        )
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
