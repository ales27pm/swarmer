"""Local operator enrollment; deliberately not exposed as an HTTP endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

from pydantic import AnyHttpUrl

from app.models import AgentCreate
from app.services.permission_policy import PermissionPolicy
from app.services.state_service import StateService


async def enroll_python_worker(
    *, db_path: Path, permissions_path: Path, credential_file: Path, model: str
) -> str:
    return await _enroll_worker(
        db_path=db_path,
        permissions_path=permissions_path,
        credential_file=credential_file,
        model=model,
        project=False,
    )


async def enroll_project_worker(
    *, db_path: Path, permissions_path: Path, credential_file: Path, model: str
) -> str:
    return await _enroll_worker(
        db_path=db_path,
        permissions_path=permissions_path,
        credential_file=credential_file,
        model=model,
        project=True,
    )


async def _enroll_worker(
    *, db_path: Path, permissions_path: Path, credential_file: Path, model: str, project: bool
) -> str:
    """Enroll under the local Unix operator identity and write the secret once.

    The caller already needs access to the private control-plane database.
    The HTTP registration route continues to require a paired device.
    """
    if not db_path.is_file():
        raise ValueError("an existing initialized control-plane database is required")
    policy = PermissionPolicy.from_yaml(permissions_path)
    credential_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = credential_file.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("credential directory must be private and operator-owned")
    descriptor = os.open(
        credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        service = StateService(db_path, permission_policy=policy)
        record = await service.register_agent(
            AgentCreate(
                name="ubuntu-project-builder" if project else "ubuntu-python-proposal-worker",
                version="0.14.0" if project else "0.13.0",
                endpoint=AnyHttpUrl("http://127.0.0.1"),
                model_id=model,
                skills=["code.build_project" if project else "code.generate_python"],
                max_concurrency=1,
                capacity={
                    "max_result_bytes": 4_000_000 if project else 524_288,
                    "max_operation_seconds": 600 if project else 120,
                },
            ),
            actor_id=f"uid:{os.getuid()}@{socket.gethostname()}",
            actor_type="operator",
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump({"agent_id": record["id"], "credential": record["credential"]}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return str(record["id"])
    finally:
        if descriptor != -1:
            os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--permissions", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--kind", choices=("python", "project"), default="python")
    args = parser.parse_args()
    agent_id = asyncio.run(
        _enroll_worker(
            db_path=args.db,
            permissions_path=args.permissions,
            credential_file=args.credential_file,
            model=args.model,
            project=args.kind == "project",
        )
    )
    print(json.dumps({"agent_id": agent_id, "credential_saved": True}))


if __name__ == "__main__":
    main()
