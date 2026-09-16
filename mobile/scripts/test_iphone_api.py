"""Run with python3 -m unittest discover -s mobile/scripts -p test_iphone_api.py.

No device discovery, app launch, installation or application command is performed.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "iphone_api", Path(__file__).with_name("iphone-api.py")
)
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)
TOKEN = "private-token-never-printed-01234567890123456789"
PASSWORD = "private-password-never-printed"
P12 = "private-p12-never-printed"
DEVICE = "00000000-0000000000000001"
UUID = "12345678-1234-1234-1234-123456789abc"
CERTIFICATE = b"peer DER certificate"


def session(**changes):
    result = {
        "version": 1,
        "tlsName": API.TLS_NAME,
        "instanceId": "abc123",
        "token": TOKEN,
        "host": "fd00::1",
        "port": 8766,
        "caPem": "public-ca-pem",
        "certificateSha256": hashlib.sha256(CERTIFICATE).hexdigest(),
        "createdAt": time.time() - 1,
        "expiresAt": time.time() + 900,
    }
    result.update(changes)
    return result


def device_result(**changes):
    result = {
        "identifier": UUID,
        "hardwareProperties": {
            "udid": DEVICE,
            "platform": "iOS",
            "reality": "physical",
        },
        "connectionProperties": {
            "pairingState": "paired",
            "tunnelState": "connected",
            "tunnelIPAddress": "fd00::1",
        },
        "deviceProperties": {"bootState": "booted", "ddiServicesAvailable": True},
    }
    result.update(changes)
    return {"result": result}


def job(state="running", **extra):
    return {
        "apiVersion": "1",
        "instanceId": "abc123",
        "job": {"id": "job_abc123_1", "state": state, **extra},
    }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"

    def test_session_is_exclusive_private_and_round_trips(self):
        value = session()
        API.save_session(self.path, value)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(API.load_session(self.path), value)
        with self.assertRaises(API.ClientError):
            API.save_session(self.path, session(token="x" * 40))
        self.assertEqual(API.load_session(self.path)["token"], TOKEN)

    def test_public_permissions_wrong_owner_and_symlinks_rejected(self):
        API.save_session(self.path, session())
        self.path.chmod(0o644)
        with self.assertRaises(API.ClientError):
            API.load_session(self.path)
        self.path.chmod(0o600)
        with (
            mock.patch.object(API.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(API.ClientError),
        ):
            API.load_session(self.path)
        link = self.path.with_name("linked.json")
        link.symlink_to(self.path)
        with self.assertRaises(API.ClientError):
            API.load_session(link)
        with self.assertRaises(API.ClientError):
            API.save_session(link, session())

    def test_expired_overlong_future_invalid_and_nonfinite_sessions_rejected(self):
        cases = [
            {"expiresAt": time.time() - 3},
            {"expiresAt": time.time() + 3601},
            {"createdAt": time.time() + 10},
            {"expiresAt": float("nan")},
            {"host": "phone.example.com"},
            {"token": "short"},
            {"token": TOKEN + "\r\nInjected: yes"},
            {"port": 80},
            {"tlsName": "wrong.local"},
            {"certificateSha256": "wrong"},
        ]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                self.path.unlink(missing_ok=True)
                API.save_session(self.path, session(**changes))
                with self.assertRaises(API.ClientError):
                    API.load_session(self.path)

    def test_first_health_binds_once_and_never_replaces_instance(self):
        value = session(instanceId=None)
        API.save_session(self.path, value)
        API.bind_instance(
            self.path, value, {"state": "ready", "instanceId": "first123"}
        )
        self.assertEqual(API.load_session(self.path)["instanceId"], "first123")
        original = self.path.read_bytes()
        API.bind_instance(
            self.path, value, {"state": "ready", "instanceId": "first123"}
        )
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(API.ClientError, "instance changed"):
            API.bind_instance(
                self.path, value, {"state": "ready", "instanceId": "reload456"}
            )
        self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_first_health_cannot_replace_another_binding(self):
        value = session(instanceId=None)
        API.save_session(self.path, value)
        delayed = dict(value)
        API.bind_instance(
            self.path, value, {"state": "ready", "instanceId": "first123"}
        )
        with self.assertRaisesRegex(API.ClientError, "instance changed"):
            API.bind_instance(
                self.path, delayed, {"state": "ready", "instanceId": "reload456"}
            )
        self.assertEqual(API.load_session(self.path)["instanceId"], "first123")

    def test_binding_rejects_replaced_credentials_and_public_file(self):
        value = session(instanceId=None)
        API.save_session(self.path, value)
        replaced = dict(value, token="x" * 40)
        with self.assertRaises(API.ClientError):
            API.bind_instance(
                self.path, replaced, {"state": "ready", "instanceId": "first123"}
            )
        self.assertIsNone(API.load_session(self.path)["instanceId"])
        self.path.chmod(0o644)
        with self.assertRaises(API.ClientError):
            API.bind_instance(
                self.path, value, {"state": "ready", "instanceId": "first123"}
            )


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        self.args = API.parser().parse_args(
            [
                "launch",
                "--device",
                DEVICE,
                "--host",
                "fd00::1",
                "--session",
                str(self.path),
            ]
        )

    def device_tool(self, argv, **kwargs):
        output = Path(argv[argv.index("--json-output") + 1])
        output.write_text(json.dumps(device_result()))
        return b""

    def test_fresh_identity_is_read_and_launch_credentials_only_in_child_env(self):
        calls = []

        def tool(argv, **kwargs):
            calls.append((argv, {**kwargs, "env": dict(kwargs["env"])}))
            if "details" in argv:
                return self.device_tool(argv, **kwargs)
            return b"private native output must not be printed"

        stdout = io.StringIO()
        with (
            mock.patch.object(API, "run_tool", side_effect=tool),
            mock.patch.object(
                API, "certificates", return_value=("ca", "a" * 64, P12, PASSWORD)
            ),
            mock.patch.object(API.secrets, "token_urlsafe", return_value=TOKEN),
            mock.patch.object(
                API,
                "request",
                return_value=(
                    200,
                    {"apiVersion": "1", "instanceId": "abc123", "state": "ready"},
                ),
            ),
            mock.patch.dict(
                os.environ, {"DEVICECTL_CHILD_UNRELATED_SECRET": "do-not-forward"}
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(
                API.main(
                    [
                        "launch",
                        "--device",
                        DEVICE,
                        "--host",
                        "fd00::1",
                        "--session",
                        str(self.path),
                    ]
                ),
                0,
            )
        self.assertEqual(len(calls), 3)
        self.assertIn("details", calls[2][0])
        self.assertIn("details", calls[0][0])
        self.assertIn(UUID, calls[1][0])
        self.assertIn("--terminate-existing", calls[1][0])
        argv_text = json.dumps([item[0] for item in calls])
        for secret in (TOKEN, PASSWORD, P12, "do-not-forward"):
            self.assertNotIn(secret, argv_text)
            self.assertNotIn(secret, stdout.getvalue())
        env = calls[1][1]["env"]
        self.assertEqual(env[API.PREFIX + "TOKEN"], TOKEN)
        self.assertEqual(env[API.PREFIX + "TLS_PASSWORD"], PASSWORD)
        self.assertEqual(env[API.PREFIX + "TLS_P12"], P12)
        self.assertNotIn("DEVICECTL_CHILD_UNRELATED_SECRET", env)
        self.assertNotIn(PASSWORD, self.path.read_text())
        self.assertNotIn(P12, self.path.read_text())
        self.assertTrue(json.loads(stdout.getvalue())["apiVerified"])
        self.assertFalse(
            Path(calls[0][0][calls[0][0].index("--json-output") + 1]).exists()
        )

    def test_launch_failure_preserves_recovery_session_without_retry(self):
        with (
            mock.patch.object(
                API,
                "read_device",
                return_value={
                    "identifier": UUID,
                    "udid": DEVICE,
                    "tunnelAddress": "fd00::1",
                },
            ),
            mock.patch.object(
                API, "certificates", return_value=("ca", "a" * 64, P12, PASSWORD)
            ),
            mock.patch.object(
                API, "run_tool", side_effect=API.ClientError("tool failed")
            ) as tool,
            self.assertRaisesRegex(API.ClientError, "uncertain; no retry"),
        ):
            API.launch(self.args)
        self.assertEqual(tool.call_count, 1)
        self.assertTrue(self.path.exists())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_wrong_identity_or_disconnected_device_cannot_launch(self):
        for value in (
            device_result(identifier="wrong", hardwareProperties={"udid": "wrong"}),
            device_result(
                connectionProperties={
                    "pairingState": "paired",
                    "tunnelState": "disconnected",
                }
            ),
            device_result(
                deviceProperties={"bootState": "booted", "ddiServicesAvailable": False}
            ),
        ):

            def tool(argv, value=value, **_):
                Path(argv[argv.index("--json-output") + 1]).write_text(
                    json.dumps(value)
                )

            with (
                mock.patch.object(API, "run_tool", side_effect=tool),
                self.assertRaises(API.ClientError),
            ):
                API.read_device(DEVICE, Path(self.directory.name))

    def test_host_and_ttl_validation_happens_before_device_or_crypto(self):
        for field, value in (
            ("host", "phone.local"),
            ("ttl", 3601),
            ("ttl", 0),
            ("port", 1),
        ):
            with self.subTest(field=field, value=value):
                args = API.parser().parse_args(
                    [
                        "launch",
                        "--device",
                        DEVICE,
                        "--host",
                        "fd00::1",
                        "--session",
                        str(self.path),
                    ]
                )
                setattr(args, field, value)
                with (
                    mock.patch.object(API, "read_device") as read,
                    self.assertRaises(API.ClientError),
                ):
                    API.launch(args)
                read.assert_not_called()

    def test_tool_errors_never_echo_credentials(self):
        for failure in (
            subprocess.CompletedProcess(["tool"], 1, TOKEN.encode(), PASSWORD.encode()),
            subprocess.TimeoutExpired(["tool"], 1, TOKEN.encode(), PASSWORD.encode()),
        ):
            kwargs = (
                {"side_effect": failure}
                if isinstance(failure, Exception)
                else {"return_value": failure}
            )
            with mock.patch.object(API.subprocess, "run", **kwargs):
                with self.assertRaises(API.ClientError) as raised:
                    API.run_tool(["tool"])
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertNotIn(PASSWORD, str(raised.exception))


class ConsoleLaunchTests(unittest.TestCase):
    setUp = LaunchTests.setUp

    def console_context(self, monitor, snapshots=None):
        stack = contextlib.ExitStack()
        snapshots = snapshots or [
            {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"},
            {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd01::1"},
        ]
        stack.enter_context(
            mock.patch.object(API, "read_device", side_effect=snapshots)
        )
        stack.enter_context(
            mock.patch.object(
                API, "certificates", return_value=("ca", "a" * 64, P12, PASSWORD)
            )
        )
        stack.enter_context(
            mock.patch.object(API.secrets, "token_urlsafe", return_value=TOKEN)
        )
        self.spawn_calls = []

        def spawn(argv, **kwargs):
            self.spawn_calls.append(
                (list(argv), {**kwargs, "env": dict(kwargs["env"])})
            )
            return monitor

        stack.enter_context(
            mock.patch.object(API.subprocess, "Popen", side_effect=spawn)
        )
        self.args.console = True
        self.args.device_tunnel = True
        self.args.host = None
        return stack

    def monitor(self):
        monitor = mock.Mock(pid=12345, returncode=None)
        monitor.poll.side_effect = lambda: monitor.returncode

        def wait(**_):
            monitor.returncode = 0
            return 0

        monitor.wait.side_effect = wait
        return monitor

    def test_console_launches_once_refreshes_route_and_prints_ready_before_wait(self):
        monitor = self.monitor()
        stdout = io.StringIO()

        def health(value, method, path, **_):
            self.assertEqual(value["host"], "fd01::1")
            self.assertEqual((method, path), ("GET", "/v1/health"))
            return 200, {"apiVersion": "1", "instanceId": "abc123", "state": "ready"}

        def wait(**_):
            # Another CLI invocation can already load the bound private session here.
            self.assertEqual(API.load_session(self.path)["instanceId"], "abc123")
            self.assertEqual(json.loads(stdout.getvalue())["event"], "ready")
            monitor.returncode = 0
            return 0

        monitor.wait.side_effect = wait
        with (
            self.console_context(monitor),
            mock.patch.object(API, "request", side_effect=health) as request,
            contextlib.redirect_stdout(stdout),
        ):
            result = API.launch(self.args)
        self.assertEqual(result["reason"], "monitor_exited")
        self.assertEqual(len(self.spawn_calls), 1)
        argv, options = self.spawn_calls[0]
        self.assertIn("--console", argv)
        self.assertNotIn("--timeout", argv)
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        for secret in (TOKEN, PASSWORD, P12):
            self.assertNotIn(secret, json.dumps(argv) + stdout.getvalue())
        self.assertEqual(options["env"][API.PREFIX + "TOKEN"], TOKEN)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(Path(argv[argv.index("--json-output") + 1]).parent.exists())
        monitor.kill.assert_not_called()
        monitor.terminate.assert_not_called()
        monitor.send_signal.assert_not_called()

    def test_wrong_postlaunch_identity_kills_only_monitor_and_never_contacts_phone_api(
        self,
    ):
        monitor = self.monitor()
        snapshots = [
            {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"},
            {"identifier": UUID, "udid": "wrong", "tunnelAddress": "fd01::1"},
        ]
        with (
            self.console_context(monitor, snapshots),
            mock.patch.object(API, "request") as request,
            self.assertRaisesRegex(API.ClientError, "identity changed"),
        ):
            API.launch(self.args)
        request.assert_not_called()
        self.assertEqual(len(self.spawn_calls), 1)
        monitor.kill.assert_called_once()
        monitor.terminate.assert_not_called()
        self.assertNotIn("instanceId", json.loads(self.path.read_text()))

    def test_console_exiting_before_health_never_relaunches(self):
        monitor = self.monitor()
        monitor.returncode = 1
        with (
            self.console_context(monitor),
            mock.patch.object(API, "request") as request,
            self.assertRaisesRegex(API.ClientError, "before HTTPS"),
        ):
            API.launch(self.args)
        request.assert_not_called()
        self.assertEqual(len(self.spawn_calls), 1)
        monitor.kill.assert_not_called()

    def test_keyboard_interrupt_cleans_local_child_and_restores_signal_handlers(self):
        monitor = self.monitor()
        handlers = {
            name: API.signal.getsignal(name)
            for name in (API.signal.SIGTERM, API.signal.SIGHUP)
        }
        with (
            self.console_context(monitor),
            mock.patch.object(API, "request", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            API.launch(self.args)
        monitor.kill.assert_called_once()
        monitor.terminate.assert_not_called()
        self.assertEqual(
            handlers, {name: API.signal.getsignal(name) for name in handlers}
        )

    def test_expiry_is_bounded_without_app_signal_or_request(self):
        monitor = self.monitor()
        value = session(expiresAt=time.time() - 1)
        with mock.patch.object(API, "request") as request:
            result = API.hold_console(monitor, value, self.path, time.monotonic() - 1)
            API.close_console(monitor)
        self.assertEqual(result["reason"], "session_expired")
        request.assert_not_called()
        monitor.kill.assert_called_once()
        monitor.terminate.assert_not_called()
        monitor.send_signal.assert_not_called()

    def test_explicit_lan_route_preserved_and_bound_session_cannot_be_refreshed(self):
        value = session(host="192.0.2.4", instanceId=None)
        API.save_session(self.path, value)
        initial = {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"}
        fresh = dict(initial, tunnelAddress="fd01::1")
        API.refresh_launch_route(self.path, value, initial, fresh)
        self.assertEqual(API.load_session(self.path)["host"], "192.0.2.4")
        API.bind_instance(self.path, value, {"state": "ready", "instanceId": "abc123"})
        before = self.path.read_bytes()
        with self.assertRaisesRegex(API.ClientError, "concurrently"):
            API.refresh_launch_route(self.path, value, initial, fresh)
        self.assertEqual(before, self.path.read_bytes())

    def test_failed_initial_health_rediscovers_only_same_device_without_relaunch(self):
        monitor = self.monitor()
        initial = {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"}
        snapshots = [
            initial,
            dict(initial, tunnelAddress="fd01::1"),
            dict(initial, tunnelAddress="fd02::1"),
        ]
        routes = []

        def health(value, method, path, **_):
            routes.append(value["host"])
            self.assertEqual((method, path), ("GET", "/v1/health"))
            if len(routes) == 1:
                raise API.ClientError("Network unavailable")
            return 200, {"apiVersion": "1", "instanceId": "abc123", "state": "ready"}

        with (
            self.console_context(monitor, snapshots),
            mock.patch.object(API, "request", side_effect=health),
            mock.patch.object(API.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            API.launch(self.args)
        self.assertEqual(routes, ["fd01::1", "fd02::1"])
        self.assertEqual(len(self.spawn_calls), 1)
        self.assertEqual(API.load_session(self.path)["host"], "fd02::1")

    def test_explicit_host_is_never_replaced_even_if_it_matched_the_initial_tunnel(
        self,
    ):
        value = session(host="fd00::1", instanceId=None, deviceTunnel=False)
        API.save_session(self.path, value)
        initial = {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"}
        API.refresh_launch_route(
            self.path, value, initial, dict(initial, tunnelAddress="fd01::1")
        )
        self.assertEqual(API.load_session(self.path)["host"], "fd00::1")

    def test_tunnel_option_replaces_required_host_and_is_mutually_exclusive(self):
        args = API.parser().parse_args(
            ["launch", "--device", DEVICE, "--device-tunnel", "--console"]
        )
        self.assertTrue(args.device_tunnel)
        self.assertIsNone(args.host)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            API.parser().parse_args(
                ["launch", "--device", DEVICE, "--device-tunnel", "--host", "fd00::1"]
            )

    def test_changed_session_during_refresh_is_preserved(self):
        value = session(instanceId=None)
        API.save_session(self.path, dict(value, token="changed-" + TOKEN))
        before = self.path.read_bytes()
        initial = {"identifier": UUID, "udid": DEVICE, "tunnelAddress": "fd00::1"}
        with self.assertRaisesRegex(API.ClientError, "concurrently"):
            API.refresh_launch_route(
                self.path, value, initial, dict(initial, tunnelAddress="fd01::1")
            )
        self.assertEqual(before, self.path.read_bytes())


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.raw = mock.Mock()
        self.secure = mock.Mock()
        self.secure.getpeercert.side_effect = lambda **kwargs: (
            CERTIFICATE
            if kwargs.get("binary_form")
            else {"subjectAltName": (("DNS", API.TLS_NAME),)}
        )
        self.context = mock.Mock()
        self.context.wrap_socket.return_value = self.secure
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.socket = self.stack.enter_context(
            mock.patch.object(API.socket, "socket", return_value=self.raw)
        )
        self.stack.enter_context(
            mock.patch.object(
                API.ssl, "create_default_context", return_value=self.context
            )
        )

    def test_explicit_ipv6_connect_uses_fixed_san_sni_and_pin(self):
        self.assertIs(API.pinned_socket(session(), 3), self.secure)
        self.socket.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        self.raw.connect.assert_called_once_with(("fd00::1", 8766, 0, 0))
        self.context.wrap_socket.assert_called_once_with(
            self.raw, server_hostname=API.TLS_NAME
        )
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.secure.getpeercert.assert_any_call(binary_form=True)

    def test_explicit_ipv4_never_resolves_tls_hostname(self):
        API.pinned_socket(session(host="192.0.2.3"), 3)
        self.socket.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        self.raw.connect.assert_called_once_with(("192.0.2.3", 8766))

    def test_pin_mismatch_closes_connection_before_authorization(self):
        with mock.patch.object(API.http.client, "HTTPConnection") as connection:
            with self.assertRaisesRegex(API.ClientError, "pin mismatch"):
                API.request(session(certificateSha256="0" * 64), "GET", "/v1/health")
            connection.return_value.request.assert_not_called()
        self.secure.close.assert_called_once()

    def test_ca_or_san_failure_sends_no_bearer(self):
        self.context.wrap_socket.side_effect = ssl.SSLCertVerificationError(
            "private TLS detail"
        )
        with mock.patch.object(API.http.client, "HTTPConnection") as connection:
            with self.assertRaises(API.ClientError) as error:
                API.request(session(), "GET", "/v1/health")
            connection.return_value.request.assert_not_called()
        self.raw.close.assert_called_once()
        self.assertNotIn("private TLS detail", str(error.exception))

    def test_cn_only_or_wrong_san_is_rejected_before_authorization(self):
        for metadata in (
            {"subject": (("commonName", API.TLS_NAME),)},
            {"subjectAltName": (("DNS", "wrong.local"),)},
        ):
            self.secure.getpeercert.side_effect = lambda metadata=metadata, **kwargs: (
                CERTIFICATE if kwargs.get("binary_form") else metadata
            )
            with (
                mock.patch.object(API.http.client, "HTTPConnection") as connection,
                self.assertRaisesRegex(API.ClientError, "SAN mismatch"),
            ):
                API.request(session(), "GET", "/v1/health")
            connection.return_value.request.assert_not_called()

    def test_authorization_sent_only_after_pin_verified(self):
        events = []
        self.secure.getpeercert.side_effect = lambda **kwargs: (
            (events.append("pin") or CERTIFICATE)
            if kwargs.get("binary_form")
            else {"subjectAltName": (("DNS", API.TLS_NAME),)}
        )
        connection = mock.Mock()
        connection.request.side_effect = lambda *a, **k: events.append("authorization")
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = (
            b'{"apiVersion":"1","instanceId":"abc123","state":"ready"}'
        )
        with mock.patch.object(
            API.http.client, "HTTPConnection", return_value=connection
        ):
            status, _ = API.request(session(), "GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(events, ["pin", "authorization"])
        self.assertEqual(
            connection.request.call_args.kwargs["headers"]["Authorization"],
            "Bearer " + TOKEN,
        )

    def test_unknown_post_response_has_key_and_never_retries(self):
        connection = mock.Mock()
        connection.getresponse.side_effect = ConnectionResetError("private data")
        with (
            mock.patch.object(
                API.http.client, "HTTPConnection", return_value=connection
            ),
            self.assertRaisesRegex(API.ClientError, "outcome unknown") as error,
        ):
            API.request(
                session(),
                "POST",
                "/v1/commands",
                {"idempotencyKey": "recovery-key-123456"},
            )
        self.assertEqual(connection.request.call_count, 1)
        self.assertIn("recovery-key-123456", str(error.exception))
        self.assertNotIn("private data", str(error.exception))

    def test_session_expiring_during_handshake_does_not_send_bearer(self):
        now = time.time()
        value = session(createdAt=now - 1, expiresAt=now + 10)
        with (
            mock.patch.object(API.time, "time", side_effect=[now, now, now + 1000]),
            mock.patch.object(API.http.client, "HTTPConnection") as connection,
        ):
            with self.assertRaises(API.ClientError):
                API.request(value, "GET", "/v1/health")
            connection.return_value.request.assert_not_called()

    def test_changed_instance_health_is_fenced_after_valid_tls(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = (
            b'{"apiVersion":"1","instanceId":"reload123","state":"ready"}'
        )
        with (
            mock.patch.object(
                API.http.client, "HTTPConnection", return_value=connection
            ),
            self.assertRaisesRegex(API.ClientError, "instance changed"),
        ):
            API.request(session(), "GET", "/v1/health")


class CommandTests(unittest.TestCase):
    def args(self, *extra):
        return API.parser().parse_args(
            ["call", "--session", "private.json", "app.status", *extra]
        )

    def test_wait_posts_once_then_only_polls_same_job(self):
        responses = [
            (202, job()),
            (200, job()),
            (200, job("succeeded", resultOmitted=True)),
        ]
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(API, "request", side_effect=responses) as request,
            mock.patch.object(API.time, "sleep"),
        ):
            response, code = API.execute(self.args("--wait"))
        self.assertEqual(code, 0)
        self.assertTrue(response["job"]["resultOmitted"])
        self.assertEqual(
            [call.args[1] for call in request.call_args_list], ["POST", "GET", "GET"]
        )
        self.assertEqual(request.call_args_list[1].args[2], "/v1/jobs/job_abc123_1")
        self.assertEqual(request.call_args_list[0].args[3]["instanceId"], "abc123")

    def test_unbound_session_refuses_post_before_any_network(self):
        with (
            mock.patch.object(
                API, "load_session", return_value=session(instanceId=None)
            ),
            mock.patch.object(API, "request") as request,
            self.assertRaisesRegex(API.ClientError, "not bound"),
        ):
            API.execute(self.args())
        request.assert_not_called()

    def test_old_job_id_cannot_alias_new_instance_job(self):
        args = API.parser().parse_args(
            ["job", "--session", "private.json", "job_old456_1"]
        )
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(API, "request") as request,
            self.assertRaisesRegex(API.ClientError, "another application instance"),
        ):
            API.execute(args)
        request.assert_not_called()

    def test_changed_instance_409_never_retries_or_rebinds(self):
        rejection = {"apiVersion": "1", "error": {"code": "session_changed"}}
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(API, "request", return_value=(409, rejection)) as request,
            mock.patch.object(API, "bind_instance") as bind,
        ):
            response, code = API.execute(self.args("--wait"))
        self.assertEqual((response, code), (rejection, 1))
        request.assert_called_once()
        bind.assert_not_called()

    def test_explicit_recovery_key_accepts_replayed_200_and_uncertain_is_terminal(self):
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(
                API, "request", return_value=(200, job("uncertain"))
            ) as request,
        ):
            _, code = API.execute(
                self.args("--wait", "--idempotency-key", "recover.same-key:123")
            )
        self.assertEqual(code, 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(
            request.call_args.args[3]["idempotencyKey"], "recover.same-key:123"
        )

    def test_unknown_command_response_is_not_automatically_retried(self):
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(
                API, "request", side_effect=API.ClientError("unknown outcome")
            ) as request,
            self.assertRaisesRegex(API.ClientError, "same-session recovery key"),
        ):
            API.execute(self.args("--wait"))
        request.assert_called_once()

    def test_wait_timeout_returns_running_receipt_and_nonzero_without_resend(self):
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(API, "request", return_value=(202, job())) as request,
        ):
            response, code = API.execute(self.args("--wait", "--wait-timeout", "0.01"))
        self.assertEqual(code, 2)
        self.assertEqual(response["job"]["state"], "running")
        request.assert_called_once()

    def test_input_payload_is_never_echoed_to_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(API, "load_session", return_value=session()),
            mock.patch.object(API, "request", return_value=(202, job())),
            mock.patch.object(
                API.sys, "stdin", io.StringIO('{"private":"never-echo-payload"}')
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(
                API.main(
                    [
                        "call",
                        "--session",
                        "private.json",
                        "app.status",
                        "--input-file",
                        "-",
                    ]
                ),
                0,
            )
        self.assertNotIn("never-echo-payload", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_invalid_json_and_nonfinite_inputs_never_reach_network(self):
        for value in ('{"value":NaN}', "[1,2]", "broken", '{"v":1e9999}'):
            with (
                mock.patch.object(API, "load_session", return_value=session()),
                mock.patch.object(API, "request") as request,
                mock.patch.object(API.sys, "stdin", io.StringIO(value)),
            ):
                with self.assertRaises(API.ClientError):
                    API.execute(self.args("--input-file", "-"))
                request.assert_not_called()


class CertificateTests(unittest.TestCase):
    def test_real_openssl_identity_has_exact_san_ca_and_der_pin(self):
        # Local crypto only. No listener, network connection or device operation.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_umask = os.umask(0o077)
            try:
                ca, pin, encoded, password = API.certificates(root)
            finally:
                os.umask(old_umask)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            for name in ("ca.key", "server.key", "server.p12"):
                self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)
            self.assertEqual(
                pin,
                hashlib.sha256(
                    ssl.PEM_cert_to_DER_cert((root / "server.pem").read_text())
                ).hexdigest(),
            )
            context = ssl.create_default_context(cadata=ca)
            self.assertTrue(context.check_hostname)
            self.assertLess(len(encoded), 65_536)
            self.assertGreaterEqual(len(password), 32)
            result = API.run_tool(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    str(root / "ca.pem"),
                    "-verify_hostname",
                    API.TLS_NAME,
                    str(root / "server.pem"),
                ]
            )
            self.assertIn(b"OK", result)
            with self.assertRaises(API.ClientError):
                API.run_tool(
                    [
                        "openssl",
                        "verify",
                        "-CAfile",
                        str(root / "ca.pem"),
                        "-verify_hostname",
                        "wrong.local",
                        str(root / "server.pem"),
                    ]
                )

    def test_real_loopback_tls_and_wrong_pin_never_sends_authorization(self):
        # A synthetic Mac loopback endpoint, never the phone/application.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca, pin, _, _ = API.certificates(root)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "server.pem", root / "server.key")
            received, failures = [], []
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(2)
                listener.settimeout(5)

                def serve():
                    try:
                        for _ in range(2):
                            raw, _ = listener.accept()
                            raw.settimeout(3)
                            with context.wrap_socket(raw, server_side=True) as peer:
                                data = b""
                                while b"\r\n\r\n" not in data:
                                    block = peer.recv(8192)
                                    if not block:
                                        break
                                    data += block
                                received.append(data)
                                if data:
                                    body = b'{"apiVersion":"1","instanceId":"abc123","state":"ready"}'
                                    peer.sendall(
                                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                                        + str(len(body)).encode()
                                        + b"\r\nConnection: close\r\n\r\n"
                                        + body
                                    )
                    except OSError as error:
                        failures.append(type(error).__name__)

                worker = threading.Thread(target=serve, daemon=True)
                worker.start()
                value = session(
                    host="127.0.0.1",
                    port=listener.getsockname()[1],
                    caPem=ca,
                    certificateSha256=pin,
                )
                status, response = API.request(value, "GET", "/v1/health")
                self.assertEqual((status, response["state"]), (200, "ready"))
                with self.assertRaisesRegex(API.ClientError, "pin mismatch"):
                    API.request(
                        dict(value, certificateSha256="0" * 64), "GET", "/v1/health"
                    )
                worker.join(timeout=6)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertIn(("Authorization: Bearer " + TOKEN).encode(), received[0])
                self.assertEqual(received[1], b"")


if __name__ == "__main__":
    unittest.main()
