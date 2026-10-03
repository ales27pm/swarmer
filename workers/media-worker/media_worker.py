"""Lease-fenced local image or French voice worker. One configured skill per process."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from media_contract import MAX_MEDIA_BYTES, MediaError, validate_media, validate_payload
from media_runtime import MediaRenderer

_PROTOCOL_PATH = (
    Path(__file__).resolve().parent.parent / "file-worker" / "file_worker.py"
)
_SPEC = importlib.util.spec_from_file_location("mongars_media_protocol", _PROTOCOL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("the shipped file-worker protocol is required")
protocol = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(protocol)
LOGGER = logging.getLogger("mongars.media_worker")
_JOB_LOCK = threading.Lock()
ARTIFACT_FIELDS = {
    "artifact_id",
    "job_id",
    "goal_id",
    "media_type",
    "size_bytes",
    "sha256",
    "width",
    "height",
    "duration_ms",
    "sample_rate",
    "channels",
}


def validate_artifact(
    value: Any, job: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ARTIFACT_FIELDS:
        raise protocol.WorkerProtocolError("invalid media artifact receipt")
    if (
        not isinstance(value["artifact_id"], str)
        or not 1 <= len(value["artifact_id"]) <= 160
        or value["job_id"] != job["id"]
    ):
        raise protocol.WorkerProtocolError("media artifact belongs to another job")
    goal = job.get("payload", {}).get("context", {}).get("goal_id")
    if goal is not None and value["goal_id"] != goal:
        raise protocol.WorkerProtocolError("media artifact belongs to another goal")
    if any(
        value[key] != expected or type(value[key]) is not type(expected)
        for key, expected in metadata.items()
    ):
        raise protocol.WorkerProtocolError(
            "media artifact receipt differs from uploaded bytes"
        )
    return dict(value)


class MediaClient(protocol.ControlPlaneClient):
    def upload_media(
        self, job_id: str, lease: Any, path: Path, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        body = path.read_bytes()
        if (
            len(body) != metadata["size_bytes"]
            or len(body) > MAX_MEDIA_BYTES
            or hashlib.sha256(body).hexdigest() != metadata["sha256"]
        ):
            raise MediaError("invalid_output")
        route = (
            f"/agents/{self._segment(self.agent_id)}/jobs/{self._segment(job_id)}/media"
        )
        request = urllib.request.Request(
            self.base_url + route,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.credential}",
                "Content-Type": metadata["media_type"],
                "X-Claim-Token": lease.claim_token,
                "X-Lease-Id": lease.lease_id,
                "X-Lease-Generation": str(lease.lease_generation),
                "X-Artifact-SHA256": metadata["sha256"],
            },
        )
        try:
            opener = urllib.request.build_opener(protocol._RejectRedirects())
            with opener.open(request, timeout=30) as response:
                return protocol._read_control_response(response, 64_000)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise protocol.LeaseLost("upload lease is stale") from exc
            if exc.code == 507:
                raise MediaError("storage_limit") from exc
            if exc.code in (400, 413, 415, 422):
                raise MediaError("invalid_output") from exc
            raise protocol.ControlPlaneUnavailable("media upload failed") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise protocol.ControlPlaneUnavailable(
                "media upload outcome unknown"
            ) from exc


def run_once(
    client: Any,
    renderer: Any,
    *,
    heartbeat_interval_seconds: float = 10,
    heartbeat_factory: Callable[..., Any] = protocol.LeaseHeartbeat,
) -> bool:
    if not 0 < heartbeat_interval_seconds <= 60:
        raise ValueError("invalid heartbeat interval")
    if not _JOB_LOCK.acquire(blocking=False):
        return False
    heartbeat = None
    try:
        if not renderer.available() or not renderer.acquire_slot():
            client.heartbeat_agent(renderer.unavailable_status)
            return False
        client.heartbeat_agent("online")
        job = client.claim()
        if job is None:
            return False
        if not isinstance(job.get("id"), str) or not job["id"]:
            raise protocol.WorkerProtocolError("missing job id")
        lease = protocol.LeaseProof.from_job(job)
        heartbeat = heartbeat_factory(
            client, job["id"], lease, heartbeat_interval_seconds
        )
        heartbeat.start()
        client.heartbeat_agent("busy")
        try:
            if job.get("required_skill") != renderer.skill:
                raise MediaError("invalid_arguments")
            payload = validate_payload(renderer.skill, job.get("payload"))
            with tempfile.TemporaryDirectory(prefix="swarmer-media-") as directory:
                path = renderer.generate(
                    payload, Path(directory), heartbeat.ensure_active
                )
                if path.parent != Path(directory) or path.name not in {
                    "output.png",
                    "output.wav",
                }:
                    raise MediaError("invalid_output")
                metadata = validate_media(path, renderer.skill, payload)
                heartbeat.ensure_active()
                client.heartbeat_job(job["id"], lease)
                receipt = client.upload_media(job["id"], lease, path, metadata)
                artifact = validate_artifact(receipt, job, metadata)
                result = {
                    "status": "completed",
                    "result": {
                        "schema_version": "1.0",
                        "content_trust": "untrusted",
                        "artifact": artifact,
                    },
                }
        except MediaError as exc:
            LOGGER.warning("media operation failed: %s", exc.code)
            result = {"status": "failed", "error": exc.code}
        except (OSError, TypeError, ValueError):
            LOGGER.warning("media operation failed: runtime_error")
            result = {"status": "failed", "error": "runtime_error"}
        heartbeat.ensure_active()
        client.heartbeat_job(job["id"], lease)
        heartbeat.ensure_active()
        client.submit_result(job["id"], lease, result)
        return True
    except (protocol.LeaseLost, protocol.LeaseUnavailable):
        LOGGER.warning("discarding media after lease loss")
        return True
    except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
        LOGGER.warning(
            "media control-plane outcome unavailable; awaiting lease recovery"
        )
        return False
    finally:
        if heartbeat is not None:
            heartbeat.stop()
            try:
                client.heartbeat_agent("online")
            except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
                LOGGER.warning("media heartbeat unavailable")
        renderer.release_slot()
        _JOB_LOCK.release()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    skill = os.environ["MONGARS_MEDIA_SKILL"]
    renderer = MediaRenderer(
        Path(os.environ["MONGARS_MEDIA_PROFILE"]),
        skill,
        timeout_seconds=float(os.environ["MONGARS_MEDIA_TIMEOUT_SECONDS"])
        if "MONGARS_MEDIA_TIMEOUT_SECONDS" in os.environ
        else None,
        max_rss_bytes=int(os.environ["MONGARS_MEDIA_MAX_RSS_BYTES"])
        if "MONGARS_MEDIA_MAX_RSS_BYTES" in os.environ
        else None,
        gpu_enabled=os.environ.get("MONGARS_MEDIA_GPU_ENABLED") == "1",
        sandbox_enabled=os.environ.get("MONGARS_MEDIA_SANDBOX") == "1",
        gpu_lock_path=Path(os.environ["MONGARS_MEDIA_GPU_LOCK"])
        if os.environ.get("MONGARS_MEDIA_GPU_LOCK")
        else None,
    )
    client = MediaClient(
        os.environ["MONGARS_SERVER_URL"],
        os.environ["MONGARS_AGENT_ID"],
        os.environ["MONGARS_AGENT_CREDENTIAL"],
    )
    while True:
        worked = run_once(client, renderer)
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    main()
