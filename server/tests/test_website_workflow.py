"""Authenticated native website project and exact reviewed preview/publication flow."""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.services.website_dossier import FetchResponse
from app.services.website_publisher import StaticDirectoryPublisher


class FixtureSource:
    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        bodies = {
            "https://atelier.example/": b'<html><title>Atelier</title><h1>Expertise locale</h1><p>Consultation 60 minutes.</p><a href="/services">Nos services</a></html>',
            "https://atelier.example/services": b'<html><title>Services</title><h1>Consultation</h1><p>Prix : 95 dollars.</p><a href="mailto:bonjour@example.org">Contact</a></html>',
        }
        body = bodies.get(url, b"missing")
        assert len(body) < max_bytes and time.monotonic() < deadline
        return FetchResponse(
            url, 200 if url in bodies else 404, {"content-type": "text/html"}, body
        )


def test_screenshot_ticket_is_hash_scoped_and_expires(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    project = command(client, paired_headers, create(client, paired_headers), "capture")
    service = client.app.state.website_workflow  # type: ignore[union-attr]
    data = service.store.get(project["id"])
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jK1sAAAAASUVORK5CYII="
    )
    sha = hashlib.sha256(content).hexdigest()
    path = service.root / data["capture_dir"] / "fixture.png"
    path.write_bytes(content)
    service._write(
        f"{data['capture_dir']}/rendered.json",
        [{"viewports": [{"screenshot": {"sha256": sha, "local_path": str(path)}}]}],
    )
    endpoint = f"/website-projects/{project['id']}/screenshots/{sha}/preview"
    assert client.post(endpoint).status_code == 401
    response = client.post(endpoint, headers=paired_headers)
    assert response.status_code == 200, response.text
    ticket = response.json()
    image = client.get(ticket["path"])
    assert image.content == content
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert client.get(ticket["path"].replace("screenshot.png", "index.html")).status_code == 404
    path.write_bytes(b"changed")
    assert client.get(ticket["path"]).status_code == 404
    path.write_bytes(content)
    with service.store.transaction() as connection:
        connection.execute("UPDATE website_preview_tokens SET expires=0")
    assert client.get(ticket["path"]).status_code == 404


def create(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    client.app.state.website_workflow.fetcher = FixtureSource()  # type: ignore[union-attr]
    response = client.post(
        "/website-projects",
        headers=headers,
        json={
            "request_id": "create-website-1",
            "source_url": "https://atelier.example/",
            "objective": "Rendre les services et leurs prix faciles à trouver.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def command(
    client: TestClient, headers: dict[str, str], project: dict[str, Any], action: str, **extra: Any
) -> dict[str, Any]:
    response = client.post(
        f"/website-projects/{project['id']}/commands",
        headers=headers,
        json={
            "request_id": f"{action}-request-{project['version']}",
            "expected_version": project["version"],
            "action": action,
            **extra,
        },
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/website-projects/{project['id']}", headers=headers).json()
        if result["status"] not in {"capturing", "branding", "building"}:
            assert result["status"] != "failed", result
            return result
        time.sleep(0.01)
    raise AssertionError("fixture workflow did not finish")


def test_website_auth_caps_and_private_create(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    assert client.get("/website-projects").status_code == 401
    caps = client.get("/website-projects/capabilities", headers=paired_headers)
    assert caps.status_code == 200
    assert caps.json()["branding_configured"] is False
    project = create(client, paired_headers)
    assert project["status"] == "draft"
    assert create(client, paired_headers)["id"] == project["id"]
    assert client.get(f"/website-projects/{project['id']}").status_code == 401
    assert not {"approval", "capture_dir", "build_file", "owner"}.intersection(project)
    denied = client.post(
        f"/website-projects/{project['id']}/commands",
        headers=paired_headers,
        json={
            "request_id": "unsafe-123",
            "expected_version": 1,
            "action": "build",
            "palette_id": "paper-ink",
        },
    )
    assert denied.status_code == 409


def test_source_content_preview_migration_and_immutable_publication(
    client: TestClient, paired_headers: dict[str, str], tmp_path: Path
) -> None:
    project = command(client, paired_headers, create(client, paired_headers), "capture")
    assert project["capture"]["pages"] == 2
    assert project["capture"]["rendered_pages"] == 0
    assert project["capture"]["render_status"][0]["status"] == "unavailable"
    dossier = client.get(
        f"/website-projects/{project['id']}/dossier", headers=paired_headers
    ).json()
    assert any("95 dollars" in item["text"] for item in dossier["inventory"])
    project = command(
        client, paired_headers, project, "build", palette_id="paper-ink", direction_id="studio"
    )
    assert project["status"] == "preview_ready"
    assert project["build"]["migration"]["unassigned_count"] == 0
    review = {"expected_version": project["version"], "build_digest": project["build"]["digest"]}
    preview = client.post(
        f"/website-projects/{project['id']}/preview", headers=paired_headers, json=review
    ).json()
    html = client.get(preview["path"])
    assert html.status_code == 200
    assert "Consultation 60 minutes" in html.text
    assert "script-src" not in html.headers["Content-Security-Policy"]
    assert "default-src 'none'" in html.headers["Content-Security-Policy"]
    assert html.headers["Referrer-Policy"] == "no-referrer"
    assert (
        client.get(preview["path"].replace("index.html", "../projects.sqlite3")).status_code == 404
    )
    root = tmp_path / "site-releases"
    root.mkdir()
    service = client.app.state.website_workflow  # type: ignore[union-attr]
    service.publisher = StaticDirectoryPublisher(root, "https://sites.example/releases")
    approval = client.post(
        f"/website-projects/{project['id']}/publication-review", headers=paired_headers, json=review
    )
    assert approval.status_code == 200, approval.text
    ticket = approval.json()
    request = {key: ticket[key] for key in ("expected_version", "build_digest", "approval_token")}
    assert (
        client.post(
            f"/website-projects/{project['id']}/publish", headers=paired_headers, json=request
        ).status_code
        == 422
    )
    published = client.post(
        f"/website-projects/{project['id']}/publish",
        headers=paired_headers,
        json={**request, "confirm_publication": True},
    )
    assert published.status_code == 200, published.text
    receipt = published.json()["publication"]
    assert receipt["digest"] == review["build_digest"]
    installed = (root / receipt["release_id"] / "index.html").read_bytes()
    assert hashlib.sha256(installed).digest() == hashlib.sha256(html.content).digest()
    replay = client.post(
        f"/website-projects/{project['id']}/publish",
        headers=paired_headers,
        json={**request, "confirm_publication": True},
    )
    assert replay.status_code == 409
    assert len([item for item in root.iterdir() if item.is_dir()]) == 1


def test_new_capture_invalidates_preview_and_stale_commands(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    project = command(client, paired_headers, create(client, paired_headers), "capture")
    project = command(client, paired_headers, project, "build", palette_id="paper-ink")
    review = {"expected_version": project["version"], "build_digest": project["build"]["digest"]}
    preview = client.post(
        f"/website-projects/{project['id']}/preview", headers=paired_headers, json=review
    ).json()
    new = command(client, paired_headers, project, "capture")
    assert new["version"] > project["version"]
    assert client.get(preview["path"]).status_code == 404
    assert (
        client.post(
            f"/website-projects/{project['id']}/preview", headers=paired_headers, json=review
        ).status_code
        == 409
    )
    response = client.post(
        f"/website-projects/{project['id']}/commands",
        headers=paired_headers,
        json={
            "request_id": "stale-command-1",
            "expected_version": project["version"],
            "action": "capture",
        },
    )
    assert response.status_code == 409


def test_capture_rejects_private_url_and_unconfigured_branding(
    client: TestClient, paired_headers: dict[str, str]
) -> None:
    response = client.post(
        "/website-projects",
        headers=paired_headers,
        json={
            "request_id": "bad-source-1",
            "source_url": "http://127.0.0.1/admin",
            "objective": "Site",
        },
    )
    assert response.status_code == 422
    project = command(client, paired_headers, create(client, paired_headers), "capture")
    response = client.post(
        f"/website-projects/{project['id']}/commands",
        headers=paired_headers,
        json={
            "request_id": "brand-source-1",
            "expected_version": project["version"],
            "action": "branding",
        },
    )
    assert response.status_code == 409
