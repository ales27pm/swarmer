"""Configuration, private storage and safe diagnostic regression coverage."""

from pathlib import Path
from typing import Any

import pytest

from app.main import _validate_runtime_boundaries
from app.services.website_branding import BrandingError, InfographicArtistClient
from app.services.website_dossier import CaptureError
from app.services.website_publisher import StaticDirectoryPublisher
from app.services.website_workflow import WebsiteWorkflow
from app.services.website_workflow_contracts import WebsiteCommand, WebsiteCreate
from app.settings import Settings


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_blank_publishing_root_environment_disables_publishing(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MONGARS_WEBSITE_PUBLISH_ROOT", value)
    settings = Settings(_env_file=None)
    assert settings.website_publish_root is None
    _validate_runtime_boundaries(settings)


def test_explicit_current_directory_publishing_root_stays_explicit() -> None:
    settings = Settings(_env_file=None, website_publish_root=Path("."))
    assert settings.website_publish_root == Path(".")
    with pytest.raises(RuntimeError, match="website_publish_root must not overlap"):
        _validate_runtime_boundaries(settings)


@pytest.mark.parametrize("overlap", ["same", "storage_nested", "public_nested"])
def test_resolved_website_storage_cannot_overlap_publication(tmp_path: Path, overlap: str) -> None:
    private = tmp_path / "private"
    private.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    storage = shared / "captures" if overlap == "storage_nested" else shared
    storage.mkdir(exist_ok=True)
    public = shared / "published" if overlap == "public_nested" else shared
    public.mkdir(exist_ok=True)
    (private / "state-website-projects").symlink_to(storage, target_is_directory=True)
    settings = Settings(
        _env_file=None,
        db_path=private / "state.db",
        workspace_root=tmp_path / "workspace",
        vector_index_path=private / "vectors",
        website_publish_root=public,
    )
    with pytest.raises(RuntimeError, match="website_publish_root must not overlap"):
        _validate_runtime_boundaries(settings)


def workflow_at(root: Path) -> WebsiteWorkflow:
    return WebsiteWorkflow(
        root,
        brand_client=InfographicArtistClient(None),
        publisher=StaticDirectoryPublisher(None, None),
    )


@pytest.mark.parametrize("suffix", ["?", "#"])
def test_publication_capability_rejects_empty_url_delimiters(tmp_path: Path, suffix: str) -> None:
    workflow = workflow_at(tmp_path / "website-private")
    workflow.publisher = StaticDirectoryPublisher(
        tmp_path, "https://preview.example/releases" + suffix
    )
    capability = workflow.capabilities()
    assert capability["publication_configured"] is False
    assert capability["publication_target"] is None


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
async def test_existing_workflow_directory_becomes_owner_only(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "website-private"
    root.mkdir()
    root.chmod(mode)
    workflow = workflow_at(root)
    try:
        workflow.initialize()
        assert root.stat().st_mode & 0o777 == 0o700
        assert workflow.store.db.exists()
    finally:
        await workflow.close()


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (ValueError("website_has_no_captured_pages"), "website_has_no_captured_pages"),
        (ValueError("invalid_asset_path"), "invalid_asset_path"),
        (CaptureError("dns_timeout"), "dns_timeout"),
        (BrandingError("branding_transport_failed"), "branding_transport_failed"),
        (ValueError("SECRET provider payload /private/company"), None),
        (CaptureError("secret_provider_payload"), None),
        (BrandingError("secret_provider_payload"), None),
        (RuntimeError("website_has_no_captured_pages"), None),
    ],
)
async def test_failed_job_preserves_only_allowlisted_diagnostic_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_reason: str | None,
) -> None:
    workflow = workflow_at(tmp_path / "website-private")
    workflow.initialize()
    try:
        project = workflow.create(
            "test-device",
            WebsiteCreate(
                request_id="failure-project",
                source_url="https://company.example/",
                objective="Refresh the company website.",
            ),
        )

        async def fail_capture(data: dict[str, Any]) -> None:
            raise failure

        monkeypatch.setattr(workflow, "_capture", fail_capture)
        workflow.command(
            "test-device",
            project["id"],
            WebsiteCommand(
                request_id="failure-capture", expected_version=project["version"], action="capture"
            ),
        )
        for task in tuple(workflow.tasks):
            await task
        failed = workflow.store.get(project["id"], "test-device")
        assert failed["status"] == "failed"
        assert "SECRET" not in failed["error"]
        assert "secret_provider_payload" not in failed["error"]
        if expected_reason:
            assert expected_reason in failed["error"]
        else:
            assert failed["error"] == (
                "L’étape a échoué. Les résultats précédents sont conservés ; "
                "réessaie ou vérifie la configuration du serveur."
            )
    finally:
        await workflow.close()
