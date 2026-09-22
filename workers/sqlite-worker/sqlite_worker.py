#!/usr/bin/env python3
"""SQLite worker using the standard authenticated, fenced job protocol.

Release must include server/app/services/sqlite_workspace.py and file-worker.
Run as a dedicated OS account owning only its approved project workspace.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("required SQLite worker release module is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


protocol = _load("sqlite_protocol", ROOT / "workers/file-worker/file_worker.py")
service = _load("sqlite_workspace", ROOT / "server/app/services/sqlite_workspace.py")
SKILLS = {
    f"database.sqlite.{operation}": operation
    for operation in ("inspect", "query", "create", "backup", "migrate")
}
LOGGER = logging.getLogger("mongars.sqlite_worker")


def run_once(base_url: str, agent_id: str, credential: str, workspace: Any) -> bool:
    client = protocol.ControlPlaneClient(base_url, agent_id, credential)
    client.heartbeat_agent("online")
    job = client.claim()
    if job is None:
        return False
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise protocol.WorkerProtocolError("claim has no job ID")
    lease = protocol.LeaseProof.from_job(job)
    heartbeat = protocol.LeaseHeartbeat(client, job_id, lease, 2)
    try:
        client.heartbeat_agent("busy")
        heartbeat.start()
        operation = SKILLS.get(job.get("required_skill"))
        try:
            if operation is None:
                raise ValueError("unsupported SQLite skill")
            result = workspace.execute(
                operation, job.get("payload"), ensure_active=heartbeat.ensure_active
            )
            result.update({"job_id": job_id, "lease_generation": lease.lease_generation})
            body = {"status": "completed", "result": result}
        except (ValueError, TypeError, OSError):
            # SQL/data/host paths must never escape via exception diagnostics.
            body = {
                "status": "failed",
                "error": "SQLite operation rejected; inspect workspace state before retry",
            }
        heartbeat.ensure_active()
        client.heartbeat_job(job_id, lease)
        client.submit_result(job_id, lease, body)
        return True
    except (protocol.LeaseLost, protocol.LeaseUnavailable):
        # A committed mutation may have happened before a network cancellation;
        # stable migration IDs prevent duplicate effects when this job is retried.
        LOGGER.warning("SQLite result lease unavailable; retry via stable migration ID")
        return True
    finally:
        heartbeat.stop()
        try:
            client.heartbeat_agent("online")
        except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
            LOGGER.warning("SQLite worker heartbeat unavailable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--deny-path",
        type=Path,
        action="append",
        required=True,
        help="Protected control-plane database/data path; repeat for each",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    credential = os.environ.get("SWARMER_WORKER_TOKEN", "")
    if not credential:
        parser.error("SWARMER_WORKER_TOKEN is required")
    workspace = service.SQLiteWorkspace(args.workspace, denied_paths=args.deny_path)
    while True:
        run_once(args.base_url, args.agent_id, credential, workspace)
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
