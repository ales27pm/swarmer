"""Bounded public resources and inert, content-addressed website media evidence."""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import warnings
from collections.abc import Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from pydantic import Field

from app.services.website_dossier import (
    USER_AGENT,
    CaptureError,
    FetchResponse,
    PublicHttpFetcher,
    WebsiteFetcher,
    normalize_public_url,
)
from app.services.website_dossier_contracts import Digest, DossierModel, UrlText


class ResourceLimits(DossierModel):
    max_resource_bytes: int = Field(default=1_048_576, ge=1_024, le=8_388_608)
    max_total_bytes: int = Field(default=8_388_608, ge=1_024, le=33_554_432)
    max_requests: int = Field(default=100, ge=1, le=200)
    max_seconds: int = Field(default=60, ge=1, le=120)
    max_redirects: int = Field(default=3, ge=0, le=5)


class AssetLimits(ResourceLimits):
    max_assets: int = Field(default=30, ge=1, le=100)
    max_image_pixels: int = Field(default=8_000_000, ge=1, le=16_000_000)
    max_preview_bytes: int = Field(default=8_388_608, ge=1_024, le=16_777_216)


class AssetReference(DossierModel):
    url: UrlText
    source_url: UrlText
    page_sha256: Digest
    source_locator: str = Field(min_length=1, max_length=100)
    kind: Literal["image", "document"]


class ResourceIssue(DossierModel):
    url: str = Field(max_length=2_048)
    reason: str = Field(max_length=100)


class DownloadedAsset(DossierModel):
    source_url: UrlText
    final_url: UrlText
    page_source_url: UrlText
    page_sha256: Digest
    source_locator: str
    fetched_at: str
    sha256: Digest
    media_type: str
    local_path: str
    size_bytes: int
    preview_local_path: str | None = None
    preview_sha256: Digest | None = None
    preview_size_bytes: int | None = None
    preview_media_type: Literal["image/png"] | None = None
    disposition: Literal["unverified_source_asset"] = "unverified_source_asset"


class AssetDownloadResult(DossierModel):
    status: Literal["completed", "partial", "unavailable"]
    assets: list[DownloadedAsset]
    issues: list[ResourceIssue]
    response_bytes: int
    requests: int


class PublicResourceSession:
    """One serialized GET/robots/redirect budget shared by all browser resources.

    Caller-supplied transports are for trusted integration/tests only. Production
    uses PublicHttpFetcher: every DNS answer is public and the chosen peer pinned.
    Scope stays on the source hostname; TLS is never downgraded on any hop.
    """

    def __init__(
        self, source_url: str, *, limits: ResourceLimits, fetcher: WebsiteFetcher | None = None
    ) -> None:
        self.source_url = normalize_public_url(source_url)
        self.limits = limits
        self.fetcher = fetcher or PublicHttpFetcher()
        self.deadline = time.monotonic() + limits.max_seconds
        self.response_bytes = 0
        self.requests = 0
        self._robots: dict[str, RobotFileParser] = {}
        self._lock = threading.Lock()

    def _scope(self, url: str, previous: str) -> str:
        url = normalize_public_url(url)
        if urlsplit(url).hostname != urlsplit(self.source_url).hostname:
            raise CaptureError("outside_origin")
        if urlsplit(url).scheme == "http" and (
            urlsplit(previous).scheme == "https" or urlsplit(self.source_url).scheme == "https"
        ):
            raise CaptureError("tls_downgrade")
        return url

    def _request(self, url: str) -> FetchResponse:
        if time.monotonic() >= self.deadline:
            raise CaptureError("duration_limit")
        if self.requests >= self.limits.max_requests:
            raise CaptureError("request_limit")
        maximum = min(
            self.limits.max_resource_bytes, self.limits.max_total_bytes - self.response_bytes
        )
        if maximum <= 0:
            raise CaptureError("total_byte_limit")
        self.requests += 1
        try:
            response = self.fetcher.get(url, max_bytes=maximum, deadline=self.deadline)
        except CaptureError as exc:
            self.response_bytes += exc.received_bytes
            raise
        except OSError as exc:
            raise CaptureError("http_unavailable") from exc
        self.response_bytes += len(response.body)
        if time.monotonic() >= self.deadline:
            raise CaptureError("duration_limit")
        if response.url != url or not 100 <= response.status <= 599:
            raise CaptureError("invalid_transport_response")
        if len(response.body) > maximum:
            raise CaptureError("response_byte_limit")
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise CaptureError("unsupported_encoding")
        return response

    def _allowed(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                response = self._follow(origin + "/robots.txt", robots=False)
                robot = RobotFileParser(origin + "/robots.txt")
                if response.status == 404:
                    robot.parse([])
                elif response.status == 200:
                    robot.parse(response.body.decode("utf-8", errors="replace").splitlines())
                else:
                    raise CaptureError("robots_unavailable")
                self._robots[origin] = robot
            except (CaptureError, ValueError) as exc:
                raise CaptureError("robots_unavailable") from exc
        if not self._robots[origin].can_fetch(USER_AGENT, url):
            raise CaptureError("robots_disallowed")

    def _follow(self, url: str, *, robots: bool, origin: str | None = None) -> FetchResponse:
        previous = origin or self.source_url
        seen: set[str] = set()
        for hop in range(self.limits.max_redirects + 1):
            url = self._scope(url, previous)
            if url in seen:
                raise CaptureError("redirect_loop")
            seen.add(url)
            if robots:
                self._allowed(url)
            response = self._request(url)
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location or hop == self.limits.max_redirects:
                raise CaptureError("redirect_limit")
            previous, url = url, urljoin(url, location)
        raise CaptureError("redirect_limit")

    def get(self, url: str, *, origin: str | None = None) -> FetchResponse:
        with self._lock:
            if origin is not None:
                self._scope(origin, self.source_url)
            return self._follow(url, robots=True, origin=origin)


def store_evidence(output_dir: Path, body: bytes, *, suffix: str) -> tuple[str, str]:
    """Write to trusted storage using exclusive creation; never overwrite symlinks."""
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(body).hexdigest()
    path = output_dir / (digest + suffix)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if (
            path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise CaptureError("artifact_path_conflict") from None
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
    return str(path.resolve()), digest


class _ReferenceParser(HTMLParser):
    def __init__(self, source_url: str, digest: str, maximum: int) -> None:
        super().__init__()
        self.source_url, self.digest, self.maximum = source_url, digest, maximum
        self.references: list[AssetReference] = []
        self.truncated = False

    def _add(self, value: str, kind: Literal["image", "document"], locator: str) -> None:
        try:
            url = normalize_public_url(urljoin(self.source_url, value))
        except ValueError:
            return
        if any(item.url == url and item.source_locator == locator for item in self.references):
            return
        if len(self.references) >= self.maximum:
            self.truncated = True
            return
        self.references.append(
            AssetReference(
                url=url,
                source_url=self.source_url,
                page_sha256=self.digest,
                source_locator=locator,
                kind=kind,
            )
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        line, offset = self.getpos()
        locator = f"{tag}@{line}:{offset}"
        if tag in {"img", "source"}:
            if values.get("src"):
                self._add(str(values["src"]), "image", locator + ":src")
            # Public HTTP(S) candidates only. Data URLs and malformed descriptors
            # are deliberately omitted, never decoded or interpreted as HTML.
            remaining = values.get("srcset") or ""
            while remaining:
                remaining = remaining.lstrip(" \t\r\n\f,")
                pieces = remaining.split(maxsplit=1)
                if not pieces:
                    break
                candidate = pieces[0]
                remaining = pieces[1] if len(pieces) > 1 else ""
                if candidate.endswith(","):
                    candidate = candidate.rstrip(",")
                else:
                    _, separator, remaining = remaining.partition(",")
                    if not separator:
                        remaining = ""
                if not candidate.startswith("data:"):
                    self._add(candidate, "image", locator + ":srcset")
        if tag == "a" and values.get("href"):
            href = str(values["href"])
            try:
                is_pdf = urlsplit(urljoin(self.source_url, href)).path.lower().endswith(".pdf")
            except ValueError:
                return
            if is_pdf:
                self._add(href, "document", locator + ":href")


def discover_asset_references(
    html: str, *, source_url: str, page_sha256: str, max_references: int = 300
) -> tuple[list[AssetReference], bool]:
    if not 1 <= max_references <= 1_000 or len(html) > 1_048_576:
        raise ValueError("reference_input_limit")
    parser = _ReferenceParser(normalize_public_url(source_url), page_sha256, max_references)
    parser.feed(html)
    parser.close()
    return parser.references, parser.truncated


def _raster_preview(body: bytes, media_type: str, limits: AssetLimits) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise CaptureError("image_decoder_unavailable") from exc
    expected = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as candidate:
                if candidate.format != expected[media_type]:
                    raise CaptureError("media_signature_mismatch")
                if candidate.width * candidate.height > limits.max_image_pixels:
                    raise CaptureError("image_pixel_limit")
                candidate.verify()
            with Image.open(io.BytesIO(body)) as candidate:
                candidate.seek(0)
                image = candidate.convert("RGBA")
                output = io.BytesIO()
                image.save(output, "PNG")
                if output.tell() > limits.max_preview_bytes:
                    raise CaptureError("preview_byte_limit")
                return output.getvalue()
    except CaptureError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise CaptureError("invalid_raster_image") from exc


def download_website_assets(
    references: Sequence[AssetReference],
    *,
    source_url: str,
    output_dir: Path,
    limits: AssetLimits | None = None,
    fetcher: WebsiteFetcher | None = None,
) -> AssetDownloadResult:
    """Preserve original bytes as inert .bin; only decoded raster PNG gets a preview.

    PDFs remain attachment evidence, never embedded, parsed, or executed here.
    A PDF signature is not a malware scan or a claim about document contents.
    """
    limits = limits or AssetLimits()
    session = PublicResourceSession(source_url, limits=limits, fetcher=fetcher)
    assets: list[DownloadedAsset] = []
    issues: list[ResourceIssue] = []
    for reference in references[: limits.max_assets]:
        try:
            session._scope(reference.source_url, source_url)
            response = session.get(reference.url, origin=reference.source_url)
            if response.status != 200:
                raise CaptureError("http_status")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            preview: bytes | None = None
            if media_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                preview = _raster_preview(response.body, media_type, limits)
            elif media_type == "application/pdf":
                if not response.body.startswith(b"%PDF-") or b"%%EOF" not in response.body[-1_024:]:
                    raise CaptureError("media_signature_mismatch")
            else:
                raise CaptureError("unsupported_media_type")
            path, digest = store_evidence(output_dir, response.body, suffix=".bin")
            preview_path, preview_digest = (
                store_evidence(output_dir, preview, suffix=".png")
                if preview is not None
                else (None, None)
            )
            assets.append(
                DownloadedAsset(
                    source_url=reference.url,
                    final_url=response.url,
                    page_source_url=reference.source_url,
                    page_sha256=reference.page_sha256,
                    source_locator=reference.source_locator,
                    fetched_at=datetime.now(UTC).isoformat(),
                    sha256=digest,
                    media_type=media_type,
                    local_path=path,
                    size_bytes=len(response.body),
                    preview_local_path=preview_path,
                    preview_sha256=preview_digest,
                    preview_size_bytes=len(preview) if preview is not None else None,
                    preview_media_type="image/png" if preview is not None else None,
                )
            )
        except (CaptureError, ValueError, OSError) as exc:
            reason = str(exc) if isinstance(exc, CaptureError) else "asset_unavailable"
            issues.append(ResourceIssue(url=reference.url, reason=reason))
    if len(references) > limits.max_assets:
        issues.append(ResourceIssue(url=source_url, reason="asset_limit"))
    return AssetDownloadResult(
        status="partial" if issues else "completed",
        assets=assets,
        issues=issues,
        response_bytes=session.response_bytes,
        requests=session.requests,
    )
