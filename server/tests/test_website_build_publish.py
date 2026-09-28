from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import posixpath
from pathlib import Path
from typing import Any

import pytest

from app.services.website_builder import (
    PALETTES,
    BrandDirection,
    BuildAsset,
    WebsiteBuild,
    WebsiteBuilder,
    canonical_json,
)
from app.services.website_dossier import FetchResponse, capture_website
from app.services.website_publisher import PublicationError, StaticDirectoryPublisher

BASE = "https://atelier.example"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="
)


class SourceFixture:
    def get(self, url: str, *, max_bytes: int, deadline: float) -> FetchResponse:
        del max_bytes, deadline
        pages = {
            BASE
            + "/": '<html lang="fr"><title>Atelier &amp; Fils</title><h1>Bienvenue</h1><p>Consultation : 80 $.</p><p>&lt;script&gt;alert(1)&lt;/script&gt;</p><img src="/logo.png" alt="Atelier"><a href="/services">Services</a><a href="mailto:bonjour@atelier.example">Écrire</a><form action="/submit" method="post"><input name="email"></form></html>',
            BASE
            + "/services": '<html><title>Services</title><h1>Services</h1><p>Ouvert lundi.</p><a href="/">Accueil</a><a href="/catalogue.pdf">Catalogue</a></html>',
        }
        body = pages.get(url)
        return FetchResponse(
            url, 200 if body else 404, {"content-type": "text/html"}, (body or "").encode()
        )


def build_site(**kwargs: Any) -> WebsiteBuild:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    return WebsiteBuilder().build(dossier, palette=PALETTES[0], **kwargs)


def test_reconstruction_accounts_for_every_item_preserves_content_and_blocks_forms() -> None:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    build = WebsiteBuilder().build(dossier, palette=PALETTES[0])
    build.verify()
    assert {i["source_item_id"] for i in build.migration["items"]} == {
        i.id for i in dossier.inventory
    }
    assert build.migration["unassigned_count"] == 0
    pages = [file.text or "" for file in build.files if file.media_type == "text/html"]
    assert len(pages) == 2
    assert any("Consultation : 80 $." in page for page in pages)
    assert any("Ouvert lundi." in page for page in pages)
    assert all("<script>" not in page and "<form" not in page for page in pages)
    assert any("&lt;script&gt;alert(1)&lt;/script&gt;" in page for page in pages)
    assert "source_forms_require_implementation" in build.readiness["blockers"]
    assert {i["reason"] for i in build.migration["items"] if i["kind"] == "image"} == {
        "asset_not_downloaded"
    }
    home = next(file.text for file in build.files if file.path == "index.html")
    assert home and 'href="pages/services-' in home
    assert "mailto:bonjour@atelier.example" in home
    assert build.strategy["marketing_analysis"]["audience"]["status"] == "unknown"
    assert build.readiness["infographic_artist_applied"] is False


def test_downloaded_media_are_content_addressed_and_not_hotlinked() -> None:
    asset = BuildAsset(
        source_url=BASE + "/logo.png",
        content_base64=base64.b64encode(PNG).decode(),
        media_type="image/png",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    build = build_site(assets=[asset])
    path = "assets/" + hashlib.sha256(PNG).hexdigest() + ".png"
    assert next(file.bytes() for file in build.files if file.path == path) == PNG
    home = next(file.text for file in build.files if file.path == "index.html")
    assert home and f'src="{path}"' in home
    assert 'src="https://' not in home
    assert (
        next(i for i in build.migration["items"] if i["kind"] == "image")["disposition"]
        == "retained"
    )


@pytest.mark.parametrize("unrelated_content", [PNG, b"not an image"])
def test_downloaded_assets_outside_content_inventory_are_ignored(
    unrelated_content: bytes,
) -> None:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    retained = BuildAsset(
        source_url=BASE + "/logo.png",
        content_base64=base64.b64encode(PNG).decode(),
        media_type="image/png",
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    unrelated = BuildAsset(
        source_url=BASE + "/background.png",
        content_base64=base64.b64encode(unrelated_content).decode(),
        media_type="image/png",
        sha256=hashlib.sha256(unrelated_content).hexdigest(),
    )
    expected = WebsiteBuilder().build(dossier, palette=PALETTES[0], assets=[retained])
    actual = WebsiteBuilder().build(dossier, palette=PALETTES[0], assets=[unrelated, retained])
    actual.verify()
    assert actual.model_dump() == expected.model_dump()


def test_build_is_deterministic_for_frozen_capture_and_changes_with_palette() -> None:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    first = WebsiteBuilder().build(dossier, palette=PALETTES[0])
    again = WebsiteBuilder().build(dossier, palette=PALETTES[0])
    different = WebsiteBuilder().build(dossier, palette=PALETTES[1])
    assert first.model_dump() == again.model_dump()
    assert first.digest != different.digest
    assert first.source_digest == different.source_digest


def test_reviewed_native_direction_changes_composition_and_binds_provenance() -> None:
    brief = {"provider": "infographic_artist", "status": "succeeded", "result": {"directions": []}}
    direction = BrandDirection(
        id="editorial",
        name="Éditorial",
        layout="editorial",
        typography="editorial",
        density="balanced",
        source="infographic_artist",
        source_result_sha256=hashlib.sha256(canonical_json(brief)).hexdigest(),
    )
    build = build_site(brand_brief=brief, direction=direction)
    assert build.readiness["infographic_artist_applied"] is True
    css = next(file.text for file in build.files if file.path == "styles.css")
    assert css and "Georgia,serif" in css and "margin-top:1.3rem" in css
    with pytest.raises(ValueError, match="brand_direction_provenance_mismatch"):
        build_site(brand_brief={**brief, "result": {}}, direction=direction)


def test_missing_content_is_flagged_and_inventory_metadata_preserved() -> None:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    dossier.pages[0].truncated_fields.append("text")
    dossier.inventory[0].page_sha256 = "0" * 64
    build = WebsiteBuilder().build(dossier, palette=PALETTES[0])
    assert "source_extraction_truncated" in build.readiness["blockers"]
    assert build.migration["items"][0]["reason"] == "source_page_digest_mismatch"
    assert build.migration["page_metadata"][0]["forms"][0]["submitted"] is False


def test_publish_writes_exact_reviewed_bytes_and_preserves_previous_release(tmp_path: Path) -> None:
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example/releases")
    first = build_site()
    receipt = publisher.publish(first, expected_digest=first.digest, release_id="project-1-r1")
    release = tmp_path / receipt["release_id"]
    for file in first.files:
        if file.path.startswith("reports/"):
            assert not (release / file.path).exists()
        else:
            assert (release / file.path).read_bytes() == file.bytes()
    public_manifest = json.loads((release / "build-manifest.json").read_text())
    assert public_manifest["reviewed_build_digest"] == first.digest
    assert all(not item["path"].startswith("reports/") for item in public_manifest["files"])
    assert receipt["deployed_digest"] == hashlib.sha256(canonical_json(public_manifest)).hexdigest()
    assert receipt["private_reports_excluded"] == 3
    assert receipt["file_count"] == len(first.files) - 3
    second = build_site(
        direction=BrandDirection(
            id="catalog",
            name="Catalogue",
            layout="catalog",
            typography="technical",
            density="balanced",
        )
    )
    other = publisher.publish(second, expected_digest=second.digest, release_id="project-1-r2")
    assert (release / "index.html").read_bytes() == next(
        f.bytes() for f in first.files if f.path == "index.html"
    )
    assert receipt["url"].endswith(receipt["release_id"] + "/index.html")
    assert (tmp_path / other["release_id"] / "index.html").exists()
    with pytest.raises(PublicationError, match="release_already_exists"):
        publisher.publish(first, expected_digest=first.digest, release_id="project-1-r1")


def test_publish_rejects_mutation_wrong_digest_and_symlinks(tmp_path: Path) -> None:
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    build = build_site()
    with pytest.raises(PublicationError, match="publication_digest_mismatch"):
        publisher.publish(build, expected_digest="0" * 64, release_id="r1")
    build.files[0].text = "mutated"
    with pytest.raises(ValueError, match="digest_mismatch"):
        publisher.publish(build, expected_digest=build.digest, release_id="r1")
    linked = tmp_path / "linked"
    target = tmp_path / "target"
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    build = build_site()
    with pytest.raises(PublicationError, match="hosting_root_missing_or_symlinked"):
        StaticDirectoryPublisher(linked, "https://preview.example").publish(
            build, expected_digest=build.digest, release_id="r1"
        )
    with pytest.raises(PublicationError, match="invalid_release_id"):
        publisher.publish(build, expected_digest=build.digest, release_id="../escape")
    assert not list(target.iterdir())


def test_unconfigured_publication_does_not_claim_success() -> None:
    build = build_site()
    with pytest.raises(PublicationError, match="website_hosting_not_configured"):
        StaticDirectoryPublisher(None, None).publish(
            build, expected_digest=build.digest, release_id="r1"
        )


def test_tampered_build_report_and_traversal_rejected(tmp_path: Path) -> None:
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    build = build_site()
    build.readiness["blockers"] = []
    with pytest.raises(ValueError, match="build_report_mismatch"):
        publisher.publish(build, expected_digest=build.digest, release_id="r1")
    build = build_site()
    build.files[0].path = "../../escape"
    with pytest.raises(ValueError, match="invalid_build_file"):
        publisher.publish(build, expected_digest=build.digest, release_id="r1")


def test_untrusted_asset_bytes_and_source_urls_rejected() -> None:
    with pytest.raises(ValueError, match="asset_type_mismatch"):
        build_site(
            assets=[
                BuildAsset(
                    source_url=BASE + "/logo.png",
                    content_base64=base64.b64encode(b"<script>").decode(),
                    media_type="image/png",
                    sha256=hashlib.sha256(b"<script>").hexdigest(),
                )
            ]
        )
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    dossier.pages[0].final_url = "javascript:alert(1)"
    with pytest.raises(ValueError, match="invalid_public_url"):
        WebsiteBuilder().build(dossier, palette=PALETTES[0])


def test_verified_rendered_inventory_is_migrated_without_relabelling_source() -> None:
    dossier = capture_website(BASE + "/", fetcher=SourceFixture())
    rendered = dossier.inventory[0].model_copy(
        update={
            "id": "rendered_fixture",
            "source_locator": "rendered_dom:mobile:p[1]",
            "kind": "text",
            "text": "Contenu ajouté par JavaScript",
            "page_sha256": "a" * 64,
        }
    )
    build = WebsiteBuilder().build(
        dossier,
        palette=PALETTES[0],
        verified_rendered_inventory=[rendered],
        business_objective="Augmenter les demandes de consultation",
    )
    record = next(i for i in build.migration["items"] if i["source_item_id"] == "rendered_fixture")
    assert record["disposition"] == "retained"
    assert record["extraction"] == "rendered_dom"
    assert "Contenu ajouté par JavaScript" in next(
        f.text or "" for f in build.files if f.path == "index.html"
    )
    assert build.migration["inventory_count"] == len(dossier.inventory) + 1
    analysis = build.strategy["marketing_analysis"]
    assert analysis["business_objective"]["text"] == "Augmenter les demandes de consultation"
    assert any(
        p["primary_action"]["target"] == "mailto:bonjour@atelier.example"
        for p in analysis["page_proposals"]
        if p["primary_action"]
    )
    assert all(i.page_sha256 != "a" * 64 for i in dossier.inventory)


def test_pdf_is_attachment_only_and_requires_host_header_configuration(tmp_path: Path) -> None:
    pdf = b"%PDF-1.7\nfixture"
    build = build_site(
        assets=[
            BuildAsset(
                source_url=BASE + "/catalogue.pdf",
                content_base64=base64.b64encode(pdf).decode(),
                media_type="application/pdf",
                sha256=hashlib.sha256(pdf).hexdigest(),
            )
        ]
    )
    attachment = next(f for f in build.files if f.path.endswith(".bin"))
    assert attachment.media_type == "application/octet-stream"
    assert all(
        "<embed" not in (f.text or "") and "<iframe" not in (f.text or "") for f in build.files
    )
    with pytest.raises(PublicationError, match="hosting_attachment_headers_not_configured"):
        StaticDirectoryPublisher(tmp_path, "https://preview.example").publish(
            build, expected_digest=build.digest, release_id="r1"
        )
    receipt = StaticDirectoryPublisher(
        tmp_path, "https://preview.example", attachment_headers_configured=True
    ).publish(build, expected_digest=build.digest, release_id="r1")
    assert (tmp_path / receipt["release_id"] / attachment.path).read_bytes() == pdf


def test_parallel_publisher_returns_busy_and_symlinked_lock_is_rejected(tmp_path: Path) -> None:
    build = build_site()
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    lock = tmp_path / ".publish.lock"
    with lock.open("wb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PublicationError, match="publication_busy"):
            publisher.publish(build, expected_digest=build.digest, release_id="r1")
    lock.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"original")
    lock.symlink_to(target)
    with pytest.raises(PublicationError, match="publication_filesystem_error"):
        publisher.publish(build, expected_digest=build.digest, release_id="r1")
    assert target.read_bytes() == b"original"


def test_build_json_schema_round_trip_and_all_local_links_resolve() -> None:
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.targets: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for key, value in attrs:
                if key in {"href", "src"} and value:
                    self.targets.append(value)

    build = build_site()
    restored = WebsiteBuild.model_validate_json(build.model_dump_json())
    restored.verify()
    schema = WebsiteBuild.model_json_schema()
    assert schema["properties"]["files"]["items"]["$ref"].endswith("BuildFile")
    paths = {file.path for file in build.files}
    for file in build.files:
        if file.media_type != "text/html":
            continue
        parser = Links()
        parser.feed(file.text or "")
        for target in parser.targets:
            if ":" in target or target.startswith("#"):
                continue
            assert posixpath.normpath(posixpath.join(posixpath.dirname(file.path), target)) in paths
    assert restored.model_dump() == build.model_dump()


def test_user_preset_with_provider_reference_is_not_claimed_as_provider_output() -> None:
    brief = {"provider": "infographic_artist", "status": "succeeded", "result": {"directions": []}}
    direction = BrandDirection(
        id="catalog",
        name="Catalogue",
        layout="catalog",
        typography="humanist",
        density="balanced",
        source="user",
        source_result_sha256=hashlib.sha256(canonical_json(brief)).hexdigest(),
    )
    build = build_site(brand_brief=brief, direction=direction)
    assert build.strategy["direction_attribution"] == "user_selection_with_provider_reference"
    assert build.readiness["brand_reference_available"] is True
    assert build.readiness["infographic_artist_applied"] is False


def test_recovery_after_atomic_rename_checks_exact_release_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    build = build_site()
    assert publisher.recover(build, expected_digest=build.digest, release_id="r1") is None
    assert list(tmp_path.iterdir()) == []
    fsync = os.fsync

    def fail_after_rename(fd: int) -> None:
        if os.fstat(fd).st_ino == tmp_path.stat().st_ino:
            raise OSError("simulated process loss after rename")
        fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_after_rename)
        with pytest.raises(PublicationError, match="publication_filesystem_error"):
            publisher.publish(build, expected_digest=build.digest, release_id="r1")
    before = {str(p): (p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*")}
    receipt = publisher.recover(build, expected_digest=build.digest, release_id="r1")
    assert receipt and receipt["digest"] == build.digest
    assert receipt["status"] == "published"
    assert {str(p): (p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.rglob("*")} == before
    with pytest.raises(PublicationError, match="release_already_exists"):
        publisher.publish(build, expected_digest=build.digest, release_id="r1")


def test_recovery_rejects_extra_mutated_or_symlinked_release_files(tmp_path: Path) -> None:
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    build = build_site()
    receipt = publisher.publish(build, expected_digest=build.digest, release_id="r1")
    release = tmp_path / receipt["release_id"]
    extra = release / "unexpected.js"
    extra.write_text("alert(1)")
    with pytest.raises(PublicationError, match="release_file_set_mismatch"):
        publisher.recover(build, expected_digest=build.digest, release_id="r1")
    extra.unlink()
    source = release / "styles.css"
    original = source.read_bytes()
    source.write_bytes(b"wrong")
    with pytest.raises(PublicationError, match="release_file_mismatch"):
        publisher.recover(build, expected_digest=build.digest, release_id="r1")
    source.unlink()
    target = tmp_path / "outside.css"
    target.write_bytes(original)
    source.symlink_to(target)
    with pytest.raises(PublicationError, match="release_verification_failed"):
        publisher.recover(build, expected_digest=build.digest, release_id="r1")


def test_publication_never_exposes_internal_strategy_and_branding_brief(tmp_path: Path) -> None:
    private = "PRIVATE STRATEGY 921735"
    build = build_site(
        business_objective=private,
        brand_brief={
            "provider": "infographic_artist",
            "status": "succeeded",
            "result": {"private": private},
        },
    )
    assert private in next(f.text or "" for f in build.files if f.path == "reports/strategy.json")
    publisher = StaticDirectoryPublisher(tmp_path, "https://preview.example")
    receipt = publisher.publish(build, expected_digest=build.digest, release_id="r1")
    release = tmp_path / receipt["release_id"]
    assert not (release / "reports").exists()
    assert all(private.encode() not in p.read_bytes() for p in release.rglob("*") if p.is_file())
    assert publisher.recover(build, expected_digest=build.digest, release_id="r1") == receipt
