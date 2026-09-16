"""Real loopback TLS qualification; no device, app credentials, or persistent Keychain changes."""

import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import selectors
import socket
import ssl
import subprocess
import sys
import tempfile
import time


def main():
    executable = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix="swarmer-automation-tls-") as folder:
        root = Path(folder)
        os.chmod(root, 0o700)
        config = root / "openssl.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1\nbasicConstraints=critical,CA:TRUE\n")
        password = secrets.token_hex(32)
        password_file = root / "password"
        password_file.write_text(password)
        os.chmod(password_file, 0o600)
        for command in [
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-config", str(config), "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem")],
            ["openssl", "pkcs12", "-export", "-inkey", str(root / "key.pem"), "-in", str(root / "cert.pem"), "-out", str(root / "identity.p12"), "-passout", "file:" + str(password_file)],
        ]:
            subprocess.run(command, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(root / "key.pem", 0o600)
        os.chmod(root / "identity.p12", 0o600)
        token = secrets.token_hex(32)
        with contextlib.closing(socket.socket()) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        environment = dict(os.environ, SWARMER_AUTOMATION_ENABLE="1", SWARMER_AUTOMATION_PORT=str(port),
                           SWARMER_AUTOMATION_TOKEN=token, SWARMER_AUTOMATION_TLS_PASSWORD=password,
                           SWARMER_AUTOMATION_TLS_P12=base64.b64encode((root / "identity.p12").read_bytes()).decode(),
                           SWARMER_AUTOMATION_TTL_SECONDS="30")
        context = ssl.create_default_context(cafile=str(root / "cert.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        expected_der = ssl.PEM_cert_to_DER_cert((root / "cert.pem").read_text())

        def host_response(process):
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(12), "Host readiness/control response timed out"
            return json.loads(process.stdout.readline())

        def connect():
            connection = context.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=3), server_hostname="localhost")
            assert connection.getpeercert(binary_form=True) == expected_der
            return connection

        def wire(route="/v1/health", bearer=token, extra="", body=None):
            method = "POST" if body is not None else "GET"
            payload = body.encode() if body is not None else b""
            headers = "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload) if body is not None else ""
            return (f"{method} {route} HTTP/1.1\r\nHost: localhost:{port}\r\nAuthorization: Bearer {bearer}\r\n{headers}{extra}\r\n").encode() + payload

        def exchange(data):
            with connect() as connection:
                connection.sendall(data)
                response = b""
                while True:
                    part = connection.recv(65536)
                    if not part:
                        break
                    response += part
            head, body = response.split(b"\r\n\r\n", 1)
            return int(head.split(b" ")[1]), json.loads(body)

        process = subprocess.Popen([executable], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            started = host_response(process)
            assert started["enabled"] is True, "TLS listener did not start"
            assert started["readyChanges"] == [True]
            status, result = exchange(wire())
            assert status == 200 and result["count"] == 1
            assert exchange(wire(bearer="wrong"))[0] == 401
            assert exchange(wire(extra="Origin: https://example.invalid\r\n"))[0] == 400
            assert exchange(wire(extra="Content-Length: 65537\r\n"))[0] == 413
            for expected in range(2, 8):
                status, result = exchange(wire("/v1/catalog"))
                assert status == 200 and result["count"] == expected, "Unauthorized request dispatched or listener stopped accepting"
            status, result = exchange(wire("/v1/commands", body='{"text":"été 📖"}'))
            assert status == 200 and json.loads(result["body"])["text"] == "été 📖"
            pending = connect()
            pending.sendall(wire("/v1/pending"))
            process.stdin.write("background\n")
            process.stdin.flush()
            stopped = host_response(process)
            assert stopped["reason"] == "background"
            assert stopped["readyChanges"] == [True, False]
            try:
                assert pending.recv(1) == b"", "Background did not close pending connection"
            except (ConnectionResetError, ssl.SSLError):
                pass
            finally:
                pending.close()
            process.stdin.write("start\n")
            process.stdin.flush()
            restarted = host_response(process)
            assert restarted["enabled"] is True
            assert restarted["readyChanges"] == [True, False, True]
            assert exchange(wire())[0] == 200
            process.stdin.write("stop\n")
            process.stdin.flush()
            stopped = host_response(process)
            assert stopped["reason"] == "stopped"
            assert stopped["readyChanges"] == [True, False, True, False]
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert process.returncode == 0

        environment["SWARMER_AUTOMATION_TTL_SECONDS"] = "1"
        process = subprocess.Popen([executable], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert host_response(process)["enabled"] is True
            time.sleep(1.2)
            process.stdin.write("start\n")
            process.stdin.flush()
            assert host_response(process) == {"enabled": False, "port": 0, "reason": "session_expired", "readyChanges": [True, False]}
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert process.returncode == 0
    print("PASS: real HTTPS identity/pin, auth before dispatch, repeated connections, UTF-8, background stop/re-enable, TTL")


if __name__ == "__main__":
    main()
