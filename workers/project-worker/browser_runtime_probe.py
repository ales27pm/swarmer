"""One fresh real browser under the caller's existing sandbox; never installs anything."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.metadata
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

HTML = b"""<!doctype html><html lang="en"><title>offline-browser-proof</title>
<button id="add">Add</button><output id="count"></output><script>
const out=document.getElementById('count');
function show(){out.textContent=localStorage.getItem('proof')||'0'};
document.getElementById('add').onclick=()=>{localStorage.setItem('proof',String(Number(out.textContent)+1));show()};show();
</script></html>"""


class BrowserFailure(RuntimeError):
    """Trusted fixture launch diagnostics, transferred only to a private artifact."""

    def __init__(self, message: str, native_log: str) -> None:
        super().__init__(message)
        self.native_log = native_log


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *_args: object) -> None:
        pass


def sandbox_proof() -> dict[str, object]:
    values = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    proof = {
        "uid": os.getuid(),
        "no_new_privileges": values["NoNewPrivs"].strip() == "1",
        "effective_capabilities_zero": int(values["CapEff"].strip(), 16) == 0,
        "root_read_only": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        "apparmor_profile": Path("/proc/self/attr/current").read_text().strip(),
        "seccomp_mode": int(values.get("Seccomp", "0")),
        "pids_limit": int(Path("/sys/fs/cgroup/pids.max").read_text().strip()),
    }
    if proof["uid"] == 0 or not all(
        proof[k]
        for k in ("no_new_privileges", "effective_capabilities_zero", "root_read_only")
    ):
        raise RuntimeError("sandbox_precondition_failed")
    interfaces = sorted(path.name for path in Path("/sys/class/net").iterdir())
    routes = [
        line.split()[0]
        for line in Path("/proc/net/route").read_text().splitlines()[1:]
        if line.strip()
    ]
    if interfaces != ["lo"] or any(interface != "lo" for interface in routes):
        raise RuntimeError("network_namespace_not_isolated")
    proof["network_interfaces"] = interfaces
    proof["non_loopback_ipv4_routes"] = 0
    with socket.socket() as client:
        client.settimeout(0.25)
        try:
            client.connect(("203.0.113.1", 443))
        except OSError:
            proof["testnet_connect_failed_only"] = True
        else:
            raise RuntimeError("external_network_available")
    return proof


def closed_failure(exc: Exception) -> str:
    text = str(exc).lower()
    if text in {
        "sandbox_precondition_failed",
        "network_namespace_not_isolated",
        "external_network_available",
    }:
        return text
    if (
        "operation not permitted" in text
        or "no usable sandbox" in text
        or "failed to move to new namespace" in text
    ):
        return "browser_sandbox_denied"
    if "executable doesn't exist" in text or "unable to obtain driver" in text:
        return "browser_binary_unavailable"
    if "timeout" in text:
        return "browser_timeout"
    if "resource temporarily unavailable" in text or "too many open files" in text:
        return "browser_resource_limit"
    if isinstance(exc, AssertionError):
        return "browser_assertion_failed"
    return "browser_launch_or_protocol_failed"


def exercise(engine: str, url: str) -> dict[str, object]:
    if engine == "selenium":
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.binary_location = "/usr/bin/chromium"
        options.add_argument("--headless=new")
        options.add_argument("--no-first-run")
        with tempfile.TemporaryDirectory(
            prefix="browser-proof-", dir="/tmp"
        ) as profile:
            options.add_argument("--user-data-dir=" + profile)
            log_path = Path(profile) / "driver.log"
            try:
                with webdriver.Chrome(
                    service=Service(
                        "/usr/bin/chromedriver",
                        log_output=str(log_path),
                        service_args=["--verbose"],
                    ),
                    options=options,
                ) as browser:
                    browser.set_page_load_timeout(10)
                    browser.get(url)
                    assert browser.title == "offline-browser-proof"
                    assert browser.find_element("id", "count").text == "0"
                    browser.find_element("id", "add").click()
                    assert browser.find_element("id", "count").text == "1"
                    browser.refresh()
                    assert browser.find_element("id", "count").text == "1"
                    return {
                        "browser_version": browser.capabilities["browserVersion"],
                        "binding_version": importlib.metadata.version("selenium"),
                    }
            except WebDriverException as exc:
                diagnostic = (
                    log_path.read_text(errors="replace")[-16000:]
                    if log_path.exists()
                    else ""
                )
                raise BrowserFailure(str(exc) + "\n" + diagnostic, diagnostic) from None
    if engine == "playwright":
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:  # noqa: SIM117 - names are acquired sequentially
            # Playwright defaults to disabling Chromium's sandbox. Require it explicitly.
            with (
                playwright.chromium.launch(
                    headless=True, chromium_sandbox=True, timeout=10000
                ) as browser,
                browser.new_context() as context,
            ):
                page = context.new_page()
                page.goto(url, timeout=10000)
                assert page.title() == "offline-browser-proof"
                assert page.locator("#count").inner_text() == "0"
                page.locator("#add").click()
                assert page.locator("#count").inner_text() == "1"
                page.reload(timeout=10000)
                assert page.locator("#count").inner_text() == "1"
                return {
                    "browser_version": browser.version,
                    "binding_version": importlib.metadata.version("playwright"),
                }
    raise ValueError("unsupported_engine")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=("selenium", "playwright"))
    args = parser.parse_args()
    started = time.monotonic()
    result: dict[str, object] = {
        "schema_version": 1,
        "engine": args.engine,
        "status": "failed",
        "fixture_sha256": hashlib.sha256(HTML).hexdigest(),
        "fresh_session_requested": True,
        "sandbox_disabled": False,
        "raw_exception_omitted": True,
    }
    server = None
    try:
        result["sandbox"] = sandbox_proof()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        result.update(exercise(args.engine, f"http://127.0.0.1:{server.server_port}/"))
        result.update(
            status="passed",
            fresh_session=True,
            assertions=[
                "fresh_storage",
                "page_title",
                "click_dom_update",
                "local_storage_survives_reload",
            ],
        )
    except Exception as exc:  # noqa: BLE001 - terminal privacy boundary for browser libraries
        result["failure_code"] = closed_failure(exc)
        result["_private_native_logs"] = (
            getattr(exc, "native_log", "") + "\n" + str(exc)
        )[-24000:]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    peak = Path("/sys/fs/cgroup/pids.peak")
    if peak.exists():
        result["pids_peak"] = int(peak.read_text().strip())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
