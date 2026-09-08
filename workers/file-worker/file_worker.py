#!/usr/bin/env python3
"""Minimal read-only remote worker for the monGARS worker protocol."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTECTED_PARTS = frozenset(
    {".env", ".git", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
)


def request(
    base_url: str,
    path: str,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> Any:
    encoded = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=encoded, method=method, headers=headers
    )
    with urllib.request.urlopen(call, timeout=30) as response:  # nosec B310 - operator-configured control plane
        raw = response.read()
    return json.loads(raw) if raw else None


def safe_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or "\0" in relative
        or any(
            part in PROTECTED_PARTS
            or "secret" in part.casefold()
            or "token" in part.casefold()
            for part in candidate.parts
        )
    ):
        raise ValueError("protected or invalid path")
    root = root.resolve(strict=True)
    target = (root / candidate).resolve(strict=True)
    if target != root and root not in target.parents:
        raise ValueError("path escapes worker root")
    return target


def execute(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    skill = job["required_skill"]
    payload = job.get("payload") or {}
    path = safe_path(root, str(payload.get("path", ".")))
    if skill == "workspace.list_dir":
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return {
            "entries": sorted(
                entry.name
                for entry in path.iterdir()
                if entry.name not in PROTECTED_PARTS
            )
        }
    if skill == "workspace.read_text":
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError("file is unavailable or too large")
        return {"content": path.read_text(encoding="utf-8")}
    raise ValueError("unsupported worker skill")


def run_once(base_url: str, agent_id: str, credential: str, root: Path) -> bool:
    request(
        base_url,
        f"/agents/{agent_id}/heartbeat",
        credential,
        "POST",
        {"status": "online"},
    )
    job = request(
        base_url, f"/agents/{agent_id}/claim", credential, "POST", {"wait_seconds": 0}
    )
    if not job:
        return False
    claim_token = job["claim_token"]
    request(
        base_url,
        f"/agents/{agent_id}/heartbeat",
        credential,
        "POST",
        {"status": "busy"},
    )
    request(
        base_url,
        f"/agents/{agent_id}/jobs/{job['id']}/heartbeat",
        credential,
        "POST",
        {"claim_token": claim_token},
    )
    try:
        result = execute(root, job)
        body = {"claim_token": claim_token, "status": "completed", "result": result}
    except (OSError, UnicodeError, ValueError) as exc:
        body = {"claim_token": claim_token, "status": "failed", "error": str(exc)[:500]}
    request(
        base_url,
        f"/agents/{agent_id}/jobs/{job['id']}/result",
        credential,
        "POST",
        body,
    )
    request(
        base_url,
        f"/agents/{agent_id}/heartbeat",
        credential,
        "POST",
        {"status": "online"},
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    base_url = os.environ["MONGARS_SERVER_URL"]
    agent_id = os.environ["MONGARS_AGENT_ID"]
    credential = os.environ["MONGARS_AGENT_CREDENTIAL"]
    root = Path(os.environ["MONGARS_WORKER_ROOT"])
    while True:
        worked = run_once(base_url, agent_id, credential, root)
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    main()
