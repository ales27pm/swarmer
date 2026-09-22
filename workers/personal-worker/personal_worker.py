#!/usr/bin/env python3
"""Lease-aware CRM integration and local document extraction worker.

No model, shell command, remote document URL or caller-selected CRM endpoint.
Deploy alongside file-worker under a restricted OS account and network policy.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import logging
import math
import os
import re
import stat
import subprocess  # nosec B404 - fixed isolated parser process
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

_PATH = Path(__file__).resolve().parents[1] / "file-worker/file_worker.py"
_SPEC = importlib.util.spec_from_file_location("personal_protocol", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("file-worker protocol is required")
protocol = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(protocol)
LOGGER = logging.getLogger("mongars.personal_worker")
SKILLS = frozenset({"crm.command", "documents.extract"})
MAX_DOCUMENT_BYTES = 20_000_000
MAX_RESULT_BYTES = 524_288
MAX_CRM_REQUEST_BYTES = 65_536
_JOB_LOCK = threading.Lock()


class PersonalError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PersonalError("invalid_limit")
    return value


def validate_crm_origin(origin: str, allowed_origins: tuple[str, ...]) -> str:
    """Both values come from operator config; neither can come from a job."""
    try:
        checked = protocol.validate_control_plane_origin(origin)
        parsed = urlsplit(checked)
    except ValueError as exc:
        raise PersonalError("crm_configuration_invalid") from exc
    if parsed.scheme != "https" or origin not in allowed_origins:
        raise PersonalError("crm_configuration_invalid")
    return origin


class CrmAdapter:
    def __init__(
        self,
        origin: str,
        token: str,
        allowed_origins: tuple[str, ...],
        *,
        timeout_seconds: float = 20,
        opener: Any = None,
    ):
        self.origin = validate_crm_origin(origin, allowed_origins)
        if (
            not isinstance(token, str)
            or len(token) < 32
            or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise PersonalError("crm_configuration_invalid")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise PersonalError("crm_configuration_invalid")
        self.token = token
        self.timeout_seconds = timeout_seconds
        # Ignore environment proxies and refuse redirects: bearer stays on one origin.
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), protocol._RejectRedirects()
        )

    def execute(self, payload: dict[str, Any], ensure_active: Callable[[], None]) -> dict[str, Any]:
        if set(payload) - {
            "operation",
            "id",
            "query",
            "limit",
            "data",
            "idempotency_key",
        }:
            raise PersonalError("crm_command_invalid")
        operation = payload.get("operation")
        if not isinstance(operation, str) or not re.fullmatch(
            r"(organizations|contacts|deals|tasks|documents)\.(search|read|create|update)",
            operation,
        ):
            raise PersonalError("crm_operation_invalid")
        writing = operation.endswith((".create", ".update"))
        key = payload.get("idempotency_key")
        if writing and (
            not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{8,128}", key)
        ):
            raise PersonalError("crm_idempotency_key_required")
        if key is not None and (
            not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{8,128}", key)
        ):
            raise PersonalError("crm_idempotency_key_invalid")
        body = {k: v for k, v in payload.items() if k != "idempotency_key"}
        encoded = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_CRM_REQUEST_BYTES:
            raise PersonalError("crm_request_too_large")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.origin + "/api/integrations/v1/commands",
            data=encoded,
            headers=headers,
            method="POST",
        )
        ensure_active()
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                if not 200 <= response.status < 300:
                    raise PersonalError("crm_http_error")
                if response.headers.get("content-encoding", "identity") not in {
                    "identity",
                    "",
                }:
                    raise PersonalError("crm_response_invalid")
                raw = response.read(MAX_RESULT_BYTES + 1)
                if time.monotonic() - started > self.timeout_seconds:
                    raise PersonalError("crm_outcome_unknown" if writing else "crm_timeout")
                if len(raw) > MAX_RESULT_BYTES:
                    raise PersonalError("crm_response_too_large")
                result = json.loads(raw)
                replayed = response.headers.get("idempotency-replayed") == "true"
        except urllib.error.HTTPError as exc:
            # No remote error body, credential or URL is emitted in job diagnostics.
            if exc.code in {400, 401, 403, 404, 409, 413}:
                raise PersonalError(f"crm_http_{exc.code}") from None
            raise PersonalError("crm_outcome_unknown" if writing else "crm_unavailable") from None
        except (
            urllib.error.URLError,
            OSError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            raise PersonalError("crm_outcome_unknown" if writing else "crm_unavailable") from exc
        if not isinstance(result, dict):
            raise PersonalError("crm_response_invalid")
        if writing:
            if (
                result.get("persisted") is not True
                or not isinstance(result.get("item"), dict)
                or not isinstance(result.get("operationId"), str)
                or result.get("resource") != operation.split(".")[0]
            ):
                raise PersonalError("crm_receipt_invalid")
        elif operation.endswith(".search"):
            if not isinstance(result.get("items"), list) or len(result["items"]) > 50:
                raise PersonalError("crm_response_invalid")
        elif not isinstance(result.get("item"), dict):
            raise PersonalError("crm_response_invalid")
        ensure_active()
        final = {
            "schema_version": "1.0",
            "content_trust": "untrusted",
            "operation": operation,
            "persisted": writing,
            "replayed": replayed,
            "receipt": result,
            "summary": "CRM command confirmed by the configured service.",
        }
        if len(json.dumps(final, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
            raise PersonalError("crm_response_too_large")
        return final


def _read_document(root: Path, relative: str) -> bytes:
    descriptor = protocol._open_beneath(root, relative, directory=False)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_BYTES:
            raise PersonalError("document_unavailable_or_too_large")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise PersonalError("document_too_large")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class DocumentExtractor:
    def __init__(self, root: Path, *, artifacts_path: Path | None = None):
        self.root = root
        self.artifacts_path = artifacts_path

    def execute(self, payload: dict[str, Any], ensure_active: Callable[[], None]) -> dict[str, Any]:
        if set(payload) - {"path", "max_characters", "max_pages"}:
            raise PersonalError("document_payload_invalid")
        relative = payload.get("path")
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise PersonalError("document_path_invalid")
        suffix = Path(relative).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md"}:
            raise PersonalError("document_format_unsupported")
        limit = _bounded_int(payload.get("max_characters"), 20_000, 1, 100_000)
        pages_limit = _bounded_int(payload.get("max_pages"), 100, 1, 200)
        ensure_active()
        raw = _read_document(self.root, relative)
        sha256 = hashlib.sha256(raw).hexdigest()
        if suffix in {".txt", ".md"}:
            text = raw.decode("utf-8")
            result = {
                "text": text[:limit],
                "partial": len(text) > limit,
                "pages": [],
                "extractor": "utf8",
                "warnings": ["output_truncated"] if len(text) > limit else [],
            }
        else:
            result = self._convert_isolated(raw, suffix, limit, pages_limit, ensure_active)
        ensure_active()
        result.update(
            {
                "schema_version": "1.0",
                "content_trust": "untrusted",
                "source": {"path": relative, "sha256": sha256, "size_bytes": len(raw)},
                "summary": "Document extraction is partial."
                if result["partial"]
                else "Document text extracted.",
            }
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
            raise PersonalError("document_result_too_large")
        return result

    def _convert_isolated(
        self,
        raw: bytes,
        suffix: str,
        limit: int,
        max_pages: int,
        ensure_active: Callable[[], None],
    ) -> dict[str, Any]:
        encoded = json.dumps(
            {
                "raw": base64.b64encode(raw).decode("ascii"),
                "suffix": suffix,
                "limit": limit,
                "max_pages": max_pages,
                "artifacts": str(self.artifacts_path) if self.artifacts_path else None,
            }
        ).encode()
        # Credentials never enter the parser child. Explicit offline mode prevents model downloads.
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "TMPDIR", "SYSTEMROOT")
            if key in os.environ
        }
        environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--convert-document"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        )  # nosec B603
        started = time.monotonic()
        first = True
        try:
            while True:
                ensure_active()
                if time.monotonic() - started > 120:
                    raise PersonalError("document_conversion_timeout")
                try:
                    output, _ = process.communicate(encoded if first else None, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if len(output) > MAX_RESULT_BYTES:
                raise PersonalError("document_result_too_large")
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError) as exc:
                raise PersonalError("document_conversion_failed") from exc
            if not isinstance(result, dict):
                raise PersonalError("document_conversion_failed")
            if process.returncode or "error" in result:
                raise PersonalError(result.get("error", "document_conversion_failed"))
            return cast(dict[str, Any], result)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def _docling(
        self,
        raw: bytes,
        suffix: str,
        limit: int,
        max_pages: int,
        ensure_active: Callable[[], None],
    ) -> dict[str, Any]:
        try:
            # Optional parser lives in its separately provisioned environment.
            InputFormat = importlib.import_module("docling.datamodel.base_models").InputFormat
            PdfPipelineOptions = importlib.import_module(
                "docling.datamodel.pipeline_options"
            ).PdfPipelineOptions
            converter = importlib.import_module("docling.document_converter")
            DocumentConverter, PdfFormatOption = (
                converter.DocumentConverter,
                converter.PdfFormatOption,
            )
            DocumentStream = importlib.import_module("docling_core.types.io").DocumentStream
        except ImportError as exc:
            raise PersonalError("docling_unavailable") from exc
        if suffix == ".pdf" and (self.artifacts_path is None or not self.artifacts_path.is_dir()):
            raise PersonalError("docling_artifacts_unavailable")
        options = PdfPipelineOptions(
            artifacts_path=self.artifacts_path,
            enable_remote_services=False,
            allow_external_plugins=False,
            do_ocr=False,
        )
        # Operators must pre-provision model artifacts. No download/enrichment services.
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )
        ensure_active()
        converted = converter.convert(
            DocumentStream(name="source" + suffix, stream=io.BytesIO(raw)),
            max_num_pages=max_pages,
            max_file_size=MAX_DOCUMENT_BYTES,
            raises_on_error=False,
        )
        ensure_active()
        status = getattr(converted.status, "value", str(converted.status))
        if status not in {"success", "partial_success"}:
            raise PersonalError("document_conversion_failed")
        text = converted.document.export_to_markdown()
        pages: list[dict[str, Any]] = []
        for item, _level in converted.document.iterate_items():
            ensure_active()
            provenance = getattr(item, "prov", [])
            if not provenance:
                continue
            refs = sorted({p.page_no for p in provenance if isinstance(p.page_no, int)})
            item_text = getattr(item, "text", "")
            if refs and isinstance(item_text, str) and item_text:
                pages.append({"pages": refs, "text": item_text[:2000]})
            if len(pages) >= 100:
                break
        warnings = []
        if suffix == ".pdf":
            warnings.append("ocr_disabled_scanned_pages_may_have_no_text")
        if status == "partial_success":
            warnings.append("conversion_partial")
        if len(text) > limit:
            warnings.append("output_truncated")
        if len(pages) >= 100:
            warnings.append("provenance_truncated")
        if not text.strip():
            warnings.append("no_text_extracted")
        return {
            "text": text[:limit],
            "partial": status == "partial_success"
            or len(text) > limit
            or not text.strip()
            or len(pages) >= 100,
            "pages": pages,
            "extractor": "docling",
            "warnings": warnings,
        }


class PersonalWorker:
    def __init__(self, documents: DocumentExtractor, crm: CrmAdapter | None = None):
        self.documents = documents
        self.crm = crm

    def execute(self, job: dict[str, Any], ensure_active: Callable[[], None]) -> dict[str, Any]:
        payload = job.get("payload")
        if not isinstance(payload, dict):
            raise PersonalError("payload_invalid")
        skill = job.get("required_skill")
        if skill == "documents.extract":
            return self.documents.execute(payload, ensure_active)
        if skill == "crm.command":
            if self.crm is None:
                raise PersonalError("crm_unconfigured")
            return self.crm.execute(payload, ensure_active)
        raise PersonalError("skill_unsupported")


def run_once(
    base_url: str,
    agent_id: str,
    credential: str,
    worker: PersonalWorker,
    *,
    heartbeat_interval_seconds: float = 10,
) -> bool:
    if not math.isfinite(heartbeat_interval_seconds) or not 0 < heartbeat_interval_seconds <= 30:
        raise ValueError("invalid heartbeat interval")
    client = protocol.ControlPlaneClient(base_url, agent_id, credential)
    if not _JOB_LOCK.acquire(blocking=False):
        return False
    heartbeat = None
    try:
        client.heartbeat_agent("online")
        job = client.claim()
        if job is None:
            return False
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise protocol.WorkerProtocolError("claim has no job id")
        lease = protocol.LeaseProof.from_job(job)
        heartbeat = protocol.LeaseHeartbeat(client, job_id, lease, heartbeat_interval_seconds)
        client.heartbeat_agent("busy")
        heartbeat.start()
        try:
            # Renew immediately before external mutation; the stable key survives a lost result.
            client.heartbeat_job(job_id, lease)
            result = worker.execute(job, heartbeat.ensure_active)
            body = {"status": "completed", "result": result}
        except PersonalError as exc:
            body = {"status": "failed", "error": exc.code}
        except (ValueError, TypeError, OSError, UnicodeError):
            body = {"status": "failed", "error": "personal_operation_failed"}
        heartbeat.ensure_active()
        client.heartbeat_job(job_id, lease)
        heartbeat.ensure_active()
        client.submit_result(job_id, lease, body)
        return True
    except (protocol.LeaseLost, protocol.LeaseUnavailable):
        LOGGER.warning("personal result suppressed: lease unavailable; retain CRM key for recovery")
        return True
    except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
        LOGGER.warning("personal control plane unavailable; leaving job for lease recovery")
        return False
    finally:
        if heartbeat is not None:
            heartbeat.stop()
            try:
                client.heartbeat_agent("online")
            except (protocol.ControlPlaneUnavailable, protocol.WorkerProtocolError):
                LOGGER.warning("personal worker online heartbeat unavailable")
        _JOB_LOCK.release()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ["MONGARS_WORKER_ROOT"]).absolute()
    artifacts = os.environ.get("MONGARS_DOCLING_ARTIFACTS_PATH")
    crm = None
    if os.environ.get("MONGARS_CRM_ORIGIN"):
        crm = CrmAdapter(
            os.environ["MONGARS_CRM_ORIGIN"],
            os.environ["MONGARS_CRM_TOKEN"],
            tuple(os.environ.get("MONGARS_CRM_ALLOWED_ORIGINS", "").split(",")),
        )
    worker = PersonalWorker(
        DocumentExtractor(root, artifacts_path=Path(artifacts) if artifacts else None),
        crm,
    )
    while True:
        worked = run_once(
            os.environ["MONGARS_SERVER_URL"],
            os.environ["MONGARS_AGENT_ID"],
            os.environ["MONGARS_AGENT_CREDENTIAL"],
            worker,
        )
        if args.once:
            return
        time.sleep(1 if worked else 5)


if __name__ == "__main__":
    if sys.argv[1:] == ["--convert-document"]:
        try:
            command = json.loads(sys.stdin.buffer.read(28_000_000))
            extractor = DocumentExtractor(
                Path("."),
                artifacts_path=Path(command["artifacts"]) if command["artifacts"] else None,
            )
            output = extractor._docling(
                base64.b64decode(command["raw"], validate=True),
                command["suffix"],
                command["limit"],
                command["max_pages"],
                lambda: None,
            )
        except PersonalError as exc:
            output = {"error": exc.code}
        except Exception:  # noqa: BLE001 - isolate parser failures without document data
            output = {"error": "document_conversion_failed"}
        print(json.dumps(output, ensure_ascii=False))
    else:
        main()
