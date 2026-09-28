"""Independent integration checks of workflow boundaries and durable side effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from app.services.website_branding import InfographicArtistClient
from app.services.website_builder import PALETTES, WebsiteBuild, canonical_json
from app.services.website_dossier import FetchResponse
from app.services.website_publisher import StaticDirectoryPublisher
from app.services.website_workflow import WebsiteWorkflow, brand_summary
from app.services.website_workflow_contracts import (
    WebsiteCommand,
    WebsiteCreate,
    WebsitePublish,
    WebsiteReview,
)
from app.services.website_workflow_store import WebsiteConflict

BASE = "https://workflow-fixture.example/"


def test_brand_summary_displays_structured_directions_without_raw_json_or_secrets() -> None:
    result = {
        "directions": {
            "result": {
                "content": [],
                "structuredContent": {
                    "view": "directions",
                    "data": {
                        "directions": [
                            {
                                "name": "Composition éditoriale",
                                "rationale": "Hiérarchie claire et espaces lisibles.",
                            }
                        ],
                        "api_key": "PRIVATE-API-KEY",
                        "credentials": {"token": "PRIVATE-TOKEN"},
                        "_meta": {"debug": "PRIVATE-METADATA"},
                    },
                },
            }
        }
    }
    summary = brand_summary(result)
    assert "Composition éditoriale" in summary
    assert "Hiérarchie claire et espaces lisibles." in summary
    assert "PRIVATE" not in summary
    assert "{" not in summary and "[" not in summary
    assert "Réponse structurée disponible" not in summary


def test_brand_summary_is_bounded_and_handles_json_content_as_readable_text() -> None:
    summary = brand_summary(
        {
            "directions": {
                "result": {
                    "structuredContent": {"data": {"directions": ["D" * 3000] * 100}},
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"name":"Rythme accessible","authorization":"PRIVATE"}\n```',
                        }
                    ],
                }
            }
        }
    )
    assert len(summary) <= 8000
    assert "D" * 2001 not in summary
    assert "Rythme accessible" in summary
    assert "PRIVATE" not in summary and "```" not in summary and "{" not in summary


@pytest.fixture
def real_source_and_mcp() -> Iterator[
    tuple[Any, InfographicArtistClient, list[str], threading.Event, threading.Event]
]:
    calls: list[str] = []
    page_started, page_gate = threading.Event(), threading.Event()
    page_gate.set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append("GET " + self.path)
            if self.path == "/":
                page_started.set()
                page_gate.wait(10)
                status, content_type, body = (
                    200,
                    "text/html",
                    (
                        b'<html lang="fr"><title>Atelier de test</title><h1>Services</h1>'
                        b'<p>Consultation sur rendez-vous.</p><a href="mailto:test@example.org">Contact</a></html>'
                    ),
                )
            else:
                status, content_type, body = 404, "text/plain", b"missing"
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = message["method"]
            calls.append(method)
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result: Any = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "search_design_systems",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer"},
                                },
                            },
                        },
                        {
                            "name": "generate_brand_directions",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    key: {}
                                    for key in (
                                        "name",
                                        "sector",
                                        "promise",
                                        "audience",
                                        "traits",
                                        "must_avoid",
                                        "risk_tolerance",
                                    )
                                },
                            },
                        },
                    ]
                }
            elif method == "tools/call":
                result = {
                    "content": [{"type": "text", "text": "Direction éditoriale sobre et lisible."}]
                }
            else:
                raise AssertionError(method)
            payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class LocalTransport:
        def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
            assert urlsplit(url).hostname == "workflow-fixture.example"
            response = httpx.get(
                f"http://127.0.0.1:{server.server_port}" + urlsplit(url).path,
                timeout=max(0.1, deadline - time.monotonic()),
                trust_env=False,
            )
            return FetchResponse(
                url, response.status_code, dict(response.headers), response.content[: max_bytes + 1]
            )

    try:
        yield (
            LocalTransport(),
            InfographicArtistClient(f"http://127.0.0.1:{server.server_port}/mcp"),
            calls,
            page_started,
            page_gate,
        )
    finally:
        page_gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def make_workflow(tmp_path: Path, fixture: Any, *, publisher: Any = None) -> WebsiteWorkflow:
    transport, client, *_ = fixture
    publish_root = tmp_path / "published"
    publish_root.mkdir(exist_ok=True)
    workflow = WebsiteWorkflow(
        tmp_path / "private",
        brand_client=client,
        publisher=publisher
        or StaticDirectoryPublisher(publish_root, "https://published.example/releases"),
        fetcher=transport,
    )
    workflow.initialize()
    return workflow


def create_project(workflow: WebsiteWorkflow) -> dict[str, Any]:
    return workflow.create(
        "owner-a",
        WebsiteCreate(
            request_id="create-review-001",
            source_url=BASE,
            objective="Présenter les services clairement.",
        ),
    )


async def run_command(
    workflow: WebsiteWorkflow, data: dict[str, Any], action: str
) -> dict[str, Any]:
    workflow.command(
        "owner-a",
        data["id"],
        WebsiteCommand(
            request_id=f"{action}-review-{data['version']}",
            expected_version=data["version"],
            action=action,
            palette_id=PALETTES[0].id if action == "build" else None,
            direction_id="editorial" if action == "build" else None,
        ),
    )
    await asyncio.gather(*workflow.tasks)
    return workflow.store.get(data["id"], "owner-a")


async def built_project(workflow: WebsiteWorkflow) -> dict[str, Any]:
    data = await run_command(workflow, create_project(workflow), "capture")
    assert data["status"] == "captured", data
    data = await run_command(workflow, data, "build")
    assert data["status"] == "preview_ready", data
    return data


async def test_real_mcp_branding_receipt_reaches_builder_with_valid_provenance(
    tmp_path: Path,
    real_source_and_mcp: Any,
) -> None:
    workflow = make_workflow(tmp_path, real_source_and_mcp)
    try:
        data = await run_command(workflow, create_project(workflow), "capture")
        data = await run_command(workflow, data, "branding")
        assert data["status"] == "awaiting_direction", data
        data = await run_command(workflow, data, "build")
        assert data["status"] == "preview_ready", data["error"]
        # The selected layout is user-chosen, with the genuine provider receipt
        # retained as a reference; it must not be invented as a provider proposal.
        assert data["build"]["readiness"]["brand_reference_available"] is True
        assert data["build"]["readiness"]["infographic_artist_applied"] is False
        stored = workflow._read(data["build_file"])
        brief = data["branding"]["result"]["directions"]
        assert stored["strategy"]["brand_brief"] == brief
        assert (
            stored["strategy"]["selected_direction"]["source_result_sha256"]
            == hashlib.sha256(canonical_json(brief)).hexdigest()
        )
        assert real_source_and_mcp[2].count("tools/call") == 2
    finally:
        await workflow.close()


async def test_second_instance_cannot_interrupt_live_first_instance_capture(
    tmp_path: Path,
    real_source_and_mcp: Any,
) -> None:
    workflow = make_workflow(tmp_path, real_source_and_mcp)
    transport, client, _, started, gate = real_source_and_mcp
    gate.clear()
    second = WebsiteWorkflow(
        workflow.root, brand_client=client, publisher=workflow.publisher, fetcher=transport
    )
    try:
        data = create_project(workflow)
        active = workflow.command(
            "owner-a",
            data["id"],
            WebsiteCommand(
                request_id="capture-review-overlap",
                expected_version=data["version"],
                action="capture",
            ),
        )
        assert await asyncio.to_thread(started.wait, 5), "actual HTTP request never started"
        try:
            second.initialize()
        except (WebsiteConflict, RuntimeError):
            # A durable exclusive process lock is one valid implementation.
            pass
        observed = workflow.store.get(data["id"], "owner-a")
        assert observed["status"] == "capturing" and observed["version"] == active["version"]
    finally:
        gate.set()
        await asyncio.gather(*workflow.tasks)
        await second.close()
        await workflow.close()


async def test_publication_completes_durably_when_request_task_is_cancelled(
    tmp_path: Path,
    real_source_and_mcp: Any,
) -> None:
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    publish_root = tmp_path / "published"
    publish_root.mkdir()

    class PausedPublisher(StaticDirectoryPublisher):
        def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            started.set()
            assert release.wait(5)
            try:
                return super().publish(*args, **kwargs)
            finally:
                completed.set()

    workflow = make_workflow(
        tmp_path,
        real_source_and_mcp,
        publisher=PausedPublisher(publish_root, "https://published.example/releases"),
    )
    try:
        data = await built_project(workflow)
        review = workflow.prepare_publication(
            "owner-a",
            data["id"],
            WebsiteReview(expected_version=data["version"], build_digest=data["build"]["digest"]),
        )
        request = WebsitePublish(
            expected_version=review["expected_version"],
            build_digest=review["build_digest"],
            approval_token=review["approval_token"],
            confirm_publication=True,
        )
        task = asyncio.create_task(workflow.publish("owner-a", data["id"], request))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(completed.wait, 5)
        for _ in range(100):
            observed = workflow.store.get(data["id"], "owner-a")
            if observed["status"] == "published":
                break
            await asyncio.sleep(0.02)
        assert list(publish_root.glob("*/index.html")), "real local publisher did not publish"
        assert (
            observed["status"] == "published"
            and observed["publication"]["digest"] == request.build_digest
        )
    finally:
        release.set()
        await workflow.close()


async def test_owner_and_review_revision_are_required_before_local_publication(
    tmp_path: Path,
    real_source_and_mcp: Any,
) -> None:
    workflow = make_workflow(tmp_path, real_source_and_mcp)
    try:
        data = await built_project(workflow)
        review = WebsiteReview(
            expected_version=data["version"], build_digest=data["build"]["digest"]
        )
        with pytest.raises(KeyError):
            workflow.prepare_publication("owner-b", data["id"], review)
        approval = workflow.prepare_publication("owner-a", data["id"], review)
        with pytest.raises(WebsiteConflict):
            await workflow.publish(
                "owner-a",
                data["id"],
                WebsitePublish(
                    expected_version=review.expected_version,
                    build_digest=review.build_digest,
                    approval_token=approval["approval_token"],
                    confirm_publication=True,
                ),
            )
        assert not list((tmp_path / "published").glob("*/index.html"))
    finally:
        await workflow.close()


async def test_capture_artifact_symlink_cannot_read_outside_private_storage(
    tmp_path: Path,
    real_source_and_mcp: Any,
) -> None:
    workflow = make_workflow(tmp_path, real_source_and_mcp)
    try:
        outside = tmp_path / "outside.json"
        outside.write_text('{"private":"must-not-read"}')
        (workflow.root / "escape.json").symlink_to(outside)
        with pytest.raises(ValueError, match="invalid_artifact_path"):
            workflow._read("escape.json")
        with pytest.raises(ValueError, match="invalid_artifact_path"):
            workflow._read("../outside.json")
    finally:
        await workflow.close()


@pytest.mark.parametrize(
    "restart_scenario", ["same", "target_changed", "root_changed", "missing", "tampered"]
)
async def test_restart_reconciles_only_exact_existing_approved_release_without_publishing(
    tmp_path: Path,
    real_source_and_mcp: Any,
    restart_scenario: str,
) -> None:
    workflow = make_workflow(tmp_path, real_source_and_mcp)
    restarted: WebsiteWorkflow | None = None
    try:
        data = await built_project(workflow)
        approval = workflow.prepare_publication(
            "owner-a",
            data["id"],
            WebsiteReview(
                expected_version=data["version"],
                build_digest=data["build"]["digest"],
            ),
        )
        current = workflow.store.get(data["id"], "owner-a")
        build = WebsiteBuild.model_validate(workflow._read(current["build_file"]))
        release_id = f"{data['id']}-{build.digest[:16]}"
        # Simulate precisely the durable crash window: approved publishing intent
        # committed, optional real atomic publish done, terminal DB receipt absent.
        current.update(
            status="publishing",
            approval=None,
            publication_intent={
                "digest": build.digest,
                "release_id": release_id,
                "target": workflow.publisher.public_base_url,
                "root": str(workflow.publisher.root.resolve()),
            },
        )
        current = workflow.store.save(current, approval["expected_version"])
        expected_receipt = None
        if restart_scenario != "missing":
            expected_receipt = workflow.publisher.publish(
                build,
                expected_digest=build.digest,
                release_id=release_id,
            )
        if restart_scenario == "tampered":
            (workflow.publisher.root / expected_receipt["release_id"] / "index.html").write_text(
                "changed after publication"
            )
        old_root = workflow.publisher.root
        before = {
            str(path.relative_to(old_root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in old_root.rglob("*")
            if path.is_file()
        }
        await workflow.close()

        class RecoverOnlyPublisher(StaticDirectoryPublisher):
            def publish(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise AssertionError("restart must never perform a new publication")

        new_root = old_root
        new_target = workflow.publisher.public_base_url
        if restart_scenario == "target_changed":
            new_target = "https://different.example/releases"
        if restart_scenario == "root_changed":
            new_root = tmp_path / "different-published"
            new_root.mkdir()
        restarted = WebsiteWorkflow(
            workflow.root,
            brand_client=workflow.brand_client,
            publisher=RecoverOnlyPublisher(new_root, new_target),
            fetcher=workflow.fetcher,
        )
        restarted.initialize()
        observed = restarted.store.get(data["id"], "owner-a")
        assert observed["version"] > current["version"]
        if restart_scenario == "same":
            assert observed["status"] == "published"
            assert observed["publication"] == expected_receipt
        else:
            assert observed["status"] == "interrupted"
            assert observed["publication"] is None
        assert observed["approval"] is None
        assert "publication_intent" not in restarted.public(observed)
        after = {
            str(path.relative_to(old_root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in old_root.rglob("*")
            if path.is_file()
        }
        assert before == after, "startup changed the old publication tree"
        if new_root != old_root:
            assert list(new_root.iterdir()) == []
    finally:
        if restarted is not None:
            await restarted.close()
        await workflow.close()
