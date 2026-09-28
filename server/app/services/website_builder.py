"""Deterministic, source-attributed static reconstruction; never executes source HTML."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import posixpath
import re
from collections import Counter
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services.website_dossier import normalize_public_url
from app.services.website_dossier_contracts import SourceInventoryItem, WebsiteDossier

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
MAX_BUILD_BYTES = 40 * 1024 * 1024
ASSET_TYPES = {
    "image/png": "png",
    "application/pdf": "bin",
}


class BuildModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrandPalette(BuildModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=100)
    background: HexColor
    surface: HexColor
    text: HexColor
    muted: HexColor
    accent: HexColor
    accent_text: HexColor


class BrandDirection(BuildModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=100)
    layout: Literal["editorial", "studio", "catalog"]
    typography: Literal["humanist", "editorial", "technical"]
    density: Literal["spacious", "balanced"]
    source: Literal["user", "infographic_artist"] = "user"
    source_result_sha256: Digest | None = None


PALETTES = [
    BrandPalette(
        id="paper-ink",
        name="Papier et encre",
        background="#F6F3EC",
        surface="#FFFFFF",
        text="#20242C",
        muted="#535E70",
        accent="#3449A0",
        accent_text="#FFFFFF",
    ),
    BrandPalette(
        id="midnight-lilac",
        name="Minuit et lilas",
        background="#171925",
        surface="#222636",
        text="#F5F4FC",
        muted="#BBBFD1",
        accent="#C6B8FF",
        accent_text="#191528",
    ),
    BrandPalette(
        id="clay-cream",
        name="Argile et crème",
        background="#FCF4EA",
        surface="#FFFFFF",
        text="#322820",
        muted="#6C5648",
        accent="#97452E",
        accent_text="#FFFFFF",
    ),
]


def _contrast(left: str, right: str) -> float:
    def luminance(color: str) -> float:
        values = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(left), luminance(right)))
    return (b + 0.05) / (a + 0.05)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def safe_build_path(path: str) -> bool:
    return bool(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,239}", path)
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


class BuildAsset(BuildModel):
    source_url: str = Field(min_length=1, max_length=2048)
    content_base64: str = Field(max_length=28 * 1024 * 1024)
    media_type: str
    sha256: Digest

    def bytes(self) -> bytes:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("invalid_asset_encoding") from exc
        if self.media_type not in ASSET_TYPES:
            raise ValueError("unsupported_asset_type")
        if self.media_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("asset_type_mismatch")
        if self.media_type == "application/pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("asset_type_mismatch")
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("asset_digest_mismatch")
        return content


class BuildFile(BuildModel):
    path: str
    media_type: str
    sha256: Digest
    size_bytes: int = Field(ge=0, le=MAX_BUILD_BYTES)
    text: str | None = None
    content_base64: str | None = None

    @model_validator(mode="after")
    def valid_file(self) -> BuildFile:
        if not safe_build_path(self.path) or (self.text is None) == (self.content_base64 is None):
            raise ValueError("invalid_build_file")
        content = self.bytes()
        if len(content) != self.size_bytes or hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("build_file_digest_mismatch")
        return self

    def bytes(self) -> bytes:
        if self.text is not None:
            return self.text.encode()
        return base64.b64decode(self.content_base64 or "", validate=True)


class WebsiteBuild(BuildModel):
    schema_version: Literal["1.0"] = "1.0"
    source_digest: Digest
    digest: Digest
    palette: BrandPalette
    files: list[BuildFile] = Field(max_length=1100)
    manifest: dict[str, Any]
    migration: dict[str, Any]
    strategy: dict[str, Any]
    readiness: dict[str, Any]

    def verify(self) -> None:
        """Revalidate mutable nested models and exact files immediately before publication."""
        checked = [BuildFile.model_validate(item.model_dump()) for item in self.files]
        if len({item.path for item in checked}) != len(checked):
            raise ValueError("duplicate_build_path")
        if sum(item.size_bytes for item in checked) > MAX_BUILD_BYTES:
            raise ValueError("build_byte_limit")
        expected = _manifest(self.source_digest, self.palette, checked)
        if (
            self.manifest != expected
            or self.digest != hashlib.sha256(canonical_json(expected)).hexdigest()
        ):
            raise ValueError("build_manifest_mismatch")
        for name, value in (
            ("migration", self.migration),
            ("strategy", self.strategy),
            ("readiness", self.readiness),
        ):
            file = next((f for f in checked if f.path == f"reports/{name}.json"), None)
            if file is None or file.bytes() != canonical_json(value):
                raise ValueError("build_report_mismatch")


def _file(path: str, content: bytes, media_type: str, *, binary: bool = False) -> BuildFile:
    return BuildFile(
        path=path,
        media_type=media_type,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        text=None if binary else content.decode(),
        content_base64=base64.b64encode(content).decode() if binary else None,
    )


def _manifest(source_digest: str, palette: BrandPalette, files: list[BuildFile]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_digest": source_digest,
        "palette": palette.model_dump(),
        "files": [
            {
                "path": f.path,
                "sha256": f.sha256,
                "size_bytes": f.size_bytes,
                "media_type": f.media_type,
            }
            for f in sorted(files, key=lambda f: f.path)
        ],
    }


def _href(value: str) -> str | None:
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"mailto", "tel"} and parsed.path and not parsed.netloc:
            return value
        if (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return value
    except ValueError:
        pass
    return None


def _relative(source_path: str, destination: str) -> str:
    return posixpath.relpath(destination, posixpath.dirname(source_path) or ".")


class WebsiteBuilder:
    def build(
        self,
        dossier: WebsiteDossier,
        *,
        palette: BrandPalette,
        assets: list[BuildAsset] | None = None,
        brand_brief: dict[str, Any] | None = None,
        direction: BrandDirection | None = None,
        verified_rendered_inventory: list[SourceInventoryItem] | None = None,
        business_objective: str | None = None,
    ) -> WebsiteBuild:
        if not dossier.pages:
            raise ValueError("website_has_no_captured_pages")
        if len(assets or []) > 100:
            raise ValueError("asset_count_limit")
        rendered_inventory = verified_rendered_inventory or []
        if len(rendered_inventory) > 15000:
            raise ValueError("rendered_inventory_limit")
        if any(
            not i.source_locator.startswith("rendered_dom:") or not i.id.startswith("rendered_")
            for i in rendered_inventory
        ):
            raise ValueError("invalid_rendered_inventory")
        inventory = [*dossier.inventory, *rendered_inventory]
        rendered_ids = {i.id for i in rendered_inventory}
        if len({i.id for i in inventory}) != len(inventory):
            raise ValueError("duplicate_source_inventory_id")
        if business_objective is not None and len(business_objective) > 8000:
            raise ValueError("business_objective_length_limit")
        normalize_public_url(dossier.source_url)
        for page in dossier.pages:
            normalize_public_url(page.final_url)
            normalize_public_url(page.requested_url)
        native_brand_applied = False
        brand_reference_available = bool(
            brand_brief
            and brand_brief.get("provider") == "infographic_artist"
            and brand_brief.get("status") == "succeeded"
        )
        if brand_brief is not None and len(canonical_json(brand_brief)) > 1024 * 1024:
            raise ValueError("brand_brief_byte_limit")
        if (
            direction is not None
            and direction.source_result_sha256 is not None
            and (
                not brand_brief
                or direction.source_result_sha256
                != hashlib.sha256(canonical_json(brand_brief)).hexdigest()
            )
        ):
            raise ValueError("brand_direction_provenance_mismatch")
        if direction is not None and direction.source == "infographic_artist":
            if (
                not brand_brief
                or brand_brief.get("provider") != "infographic_artist"
                or brand_brief.get("status") != "succeeded"
                or direction.source_result_sha256
                != hashlib.sha256(canonical_json(brand_brief)).hexdigest()
            ):
                raise ValueError("brand_direction_provenance_mismatch")
            native_brand_applied = True
        source_digest = hashlib.sha256(canonical_json(dossier.model_dump())).hexdigest()
        pages = sorted(
            dossier.pages, key=lambda p: (p.requested_url != dossier.source_url, p.final_url)
        )
        paths: dict[str, str] = {}
        for index, page in enumerate(pages):
            if page.final_url in paths:
                raise ValueError("duplicate_source_page")
            slug = re.sub(r"[^a-z0-9]+", "-", urlsplit(page.final_url).path.lower()).strip("-")[:50]
            path = (
                "index.html"
                if index == 0
                else (
                    f"pages/{slug or 'page'}-{hashlib.sha256(page.final_url.encode()).hexdigest()[:10]}/index.html"
                )
            )
            paths[page.final_url] = path
            paths[page.requested_url] = path
        files: dict[str, BuildFile] = {}
        asset_paths: dict[str, str] = {}
        asset_bytes = 0
        known_asset_urls = {i.original_url for i in inventory if i.kind in {"image", "document"}}
        for asset in assets or []:
            asset_content = asset.bytes()
            asset_bytes += len(asset_content)
            if asset_bytes > MAX_BUILD_BYTES:
                raise ValueError("build_byte_limit")
            if asset.source_url not in known_asset_urls:
                raise ValueError("asset_not_in_source_inventory")
            path = f"assets/{asset.sha256}.{ASSET_TYPES[asset.media_type]}"
            if asset.source_url in asset_paths and asset_paths[asset.source_url] != path:
                raise ValueError("conflicting_asset_capture")
            asset_paths[asset.source_url] = path
            files[path] = _file(
                path,
                asset_content,
                "application/octet-stream"
                if asset.media_type == "application/pdf"
                else asset.media_type,
                binary=True,
            )
        migration_items: list[dict[str, Any]] = []
        fragments: dict[str, list[str]] = {page.final_url: [] for page in pages}
        esc = html.escape
        rendered_duplicate_targets: dict[tuple[str, str, str, str | None], str] = {}
        for item in inventory:
            page_path = paths.get(item.source_url)
            state, reason, destination = "retained", "source_content_preserved", None
            markup = ""
            anchor = "item-" + hashlib.sha256(item.id.encode()).hexdigest()[:20]
            if page_path is None:
                state, reason = "needs_confirmation", "source_page_not_captured"
            elif item.id not in rendered_ids and not any(
                p.final_url == item.source_url and p.html_sha256 == item.page_sha256 for p in pages
            ):
                state, reason = "needs_confirmation", "source_page_digest_mismatch"
            elif item.kind in {"text", "heading"}:
                tag = "h2" if item.kind == "heading" else "p"
                markup = f'<{tag} id="{anchor}">{esc(item.text)}</{tag}>'
                destination = f"{page_path}#{anchor}"
            elif item.kind in {"image", "document"}:
                local = asset_paths.get(item.original_url or "")
                if local:
                    href = esc(_relative(page_path, local), quote=True)
                    if item.kind == "image" and files[local].media_type.startswith("image/"):
                        markup = f'<figure id="{anchor}"><img src="{href}" alt="{esc(item.text, quote=True)}" loading="lazy"></figure>'
                    else:
                        markup = f'<p id="{anchor}"><a href="{href}" download="document.pdf">{esc(item.text or "Document")}</a></p>'
                    destination, reason = local, "captured_asset_preserved"
                else:
                    state, reason = "needs_confirmation", "asset_not_downloaded"
                    markup = f'<p class="source-note" id="{anchor}">{esc(item.text or item.kind)} — média à confirmer</p>'
            else:
                target = item.original_url or ""
                normalized = target.split("#", 1)[0]
                if normalized in paths:
                    destination = paths[normalized]
                    markup = f'<p id="{anchor}"><a href="{esc(_relative(page_path, destination), quote=True)}">{esc(item.text or target)}</a></p>'
                    reason = "internal_link_mapped"
                elif _href(target):
                    same_origin = urlsplit(target).netloc == urlsplit(dossier.source_url).netloc
                    if same_origin:
                        state, reason = "needs_confirmation", "internal_target_not_captured"
                    else:
                        reason = "external_or_contact_reference_preserved"
                    destination = target
                    markup = f'<p id="{anchor}"><a href="{esc(target, quote=True)}" rel="noreferrer">{esc(item.text or target)}</a></p>'
                else:
                    state, reason = "excluded", "unsafe_or_missing_link_target"
            equivalence = (item.source_url, item.kind, item.text, item.original_url)
            if item.id in rendered_ids and equivalence in rendered_duplicate_targets:
                destination = rendered_duplicate_targets[equivalence]
                state, reason, markup = "retained", "identical_source_content_already_retained", ""
            if state == "retained" and destination:
                rendered_duplicate_targets[equivalence] = destination
            if page_path is not None:
                final_url = next(p.final_url for p in pages if paths[p.final_url] == page_path)
                fragments[final_url].append(markup)
            migration_items.append(
                {
                    "source_item_id": item.id,
                    "source_url": item.source_url,
                    "source_locator": item.source_locator,
                    "page_sha256": item.page_sha256,
                    "kind": item.kind,
                    "extraction": "rendered_dom" if item.id in rendered_ids else "source_html",
                    "disposition": state,
                    "reason": reason,
                    "destination": destination,
                }
            )
        contrast = {
            "text_background": _contrast(palette.text, palette.background),
            "muted_background": _contrast(palette.muted, palette.background),
            "text_surface": _contrast(palette.text, palette.surface),
            "accent_background": _contrast(palette.accent, palette.background),
            "accent_text": _contrast(palette.accent_text, palette.accent),
        }
        blockers = []
        if dossier.coverage.status == "partial":
            blockers.append("source_capture_partial")
        if any(page.truncated_fields for page in pages):
            blockers.append("source_extraction_truncated")
        if any(page.forms for page in pages):
            blockers.append("source_forms_require_implementation")
        if any(i["disposition"] == "needs_confirmation" for i in migration_items):
            blockers.append("migration_items_need_confirmation")
        if any(value < 4.5 for value in contrast.values()):
            blockers.append("palette_contrast_below_4_5")
        migration = {
            "schema_version": "1.0",
            "source_digest": source_digest,
            "items": migration_items,
            "inventory_count": len(inventory),
            "source_html_inventory_count": len(dossier.inventory),
            "rendered_inventory_count": len(rendered_inventory),
            "accounted_count": len(migration_items),
            "unassigned_count": 0,
            "counts": dict(Counter(i["disposition"] for i in migration_items)),
            "page_map": paths,
            "page_metadata": [
                {
                    "url": p.final_url,
                    "metadata": p.metadata,
                    "structured_data": p.structured_data,
                    "forms": [f.model_dump() for f in p.forms],
                    "truncated_fields": p.truncated_fields,
                }
                for p in pages
            ],
        }
        strategy = {
            "schema_version": "1.0",
            "source_authority": "unverified_site_content",
            "selected_direction": direction.model_dump() if direction else None,
            "direction_attribution": (
                "reviewed_provider_direction"
                if native_brand_applied
                else "user_selection_with_provider_reference"
                if direction and brand_reference_available
                else "user_selection"
                if direction
                else "baseline"
            ),
            "brand_brief": brand_brief,
            "source_claims": [
                {
                    "id": i.id,
                    "text": i.text,
                    "source_url": i.source_url,
                    "page_sha256": i.page_sha256,
                }
                for i in inventory
                if i.kind in {"text", "heading"}
            ],
            "observations": {
                "captured_pages": len(pages),
                "source_headings": [h for p in pages for h in p.headings],
                "coverage": dossier.coverage.model_dump(),
            },
            "marketing_analysis": {
                "status": "proposal_requires_business_review",
                "proposals": [
                    "Valider les offres et les coordonnées reprises du site source.",
                    "Choisir une action principale par page selon l'objectif commercial.",
                    "Vérifier les contenus manquants avant toute campagne.",
                ],
                "business_objective": {"text": business_objective, "authority": "user_brief"},
                "audience": {
                    "status": "unknown",
                    "reason": "No audience has been confirmed by the company.",
                },
                "positioning": {
                    "status": "requires_confirmation",
                    "source_name": pages[0].title,
                    "observed_topics": [
                        {"text": i.text, "source_item_id": i.id}
                        for i in inventory
                        if i.kind == "heading"
                    ],
                },
                "page_proposals": _page_proposals(inventory, paths),
                "measurement_plan": [
                    "Mesurer les visites et les prises de contact après choix des outils et du consentement.",
                    "Comparer chaque parcours à l'objectif commercial validé.",
                ],
                "unknowns": [
                    "public cible prioritaire",
                    "objectifs chiffrés",
                    "différenciation confirmée",
                    "droits des médias",
                    "canaux et budget marketing",
                ],
                "note": "Aucun avantage, résultat client ou fait commercial n'est inventé.",
            },
        }
        readiness = {
            "schema_version": "1.0",
            "status": "needs_review",
            "blockers": blockers,
            "contrast_ratios": contrast,
            "automated_checks": {
                "inventory_accounted": True,
                "source_html_executed": False,
                "forms_submitted": False,
                "responsive_css": True,
            },
            "unverified": [
                "visual_review",
                "keyboard_and_screen_reader_review",
                "business_claims_review",
                "brand_direction_approval",
            ],
            "brand_source": "infographic_artist_reviewed_direction"
            if native_brand_applied
            else "explicit_palette_baseline",
            "infographic_artist_applied": native_brand_applied,
            "brand_reference_available": brand_reference_available,
        }
        css = _stylesheet(palette, direction)
        files["styles.css"] = _file("styles.css", css.encode(), "text/css")
        for page in pages:
            path = paths[page.final_url]
            nav = "".join(
                f'<a href="{esc(_relative(path, paths[p.final_url]), quote=True)}"'
                f"{' aria-current="page"' if p.final_url == page.final_url else ''}>"
                f"{esc(p.title or (p.headings[0] if p.headings else 'Page'))}</a>"
                for p in pages
            )
            title = page.title or (page.headings[0] if page.headings else "Page")
            description = page.metadata.get("description", "")
            content = "\n".join(fragments[page.final_url])
            form_note = (
                "<aside>Les formulaires du site source nécessitent une configuration.</aside>"
                if page.forms
                else ""
            )
            document = f'''<!doctype html>
<html lang="{esc(page.language or "fr", quote=True)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'">
<title>{esc(title)}</title><meta name="description" content="{esc(description, quote=True)}">
<link rel="stylesheet" href="{esc(_relative(path, "styles.css"), quote=True)}"></head>
<body><a class="skip" href="#main">Aller au contenu</a><header><nav aria-label="Navigation principale">{nav}</nav></header>
<main id="main"><h1>{esc(title)}</h1><section aria-label="Contenu repris du site source">{content}</section>
{form_note}<details><summary>Texte intégral extrait</summary><div class="transcript">{esc(page.text)}</div></details></main>
<footer><a href="{esc(page.final_url, quote=True)}" rel="noreferrer">Source du contenu</a></footer></body></html>'''
            files[path] = _file(path, document.encode(), "text/html")
        for name, report in (
            ("migration", migration),
            ("strategy", strategy),
            ("readiness", readiness),
        ):
            files[f"reports/{name}.json"] = _file(
                f"reports/{name}.json", canonical_json(report), "application/json"
            )
        ordered = sorted(files.values(), key=lambda f: f.path)
        manifest = _manifest(source_digest, palette, ordered)
        build = WebsiteBuild(
            source_digest=source_digest,
            digest=hashlib.sha256(canonical_json(manifest)).hexdigest(),
            palette=palette,
            files=ordered,
            manifest=manifest,
            migration=migration,
            strategy=strategy,
            readiness=readiness,
        )
        build.verify()
        return build


def _page_proposals(
    inventory: list[SourceInventoryItem], paths: dict[str, str]
) -> list[dict[str, Any]]:
    proposals = []
    for source_url in dict.fromkeys(item.source_url for item in inventory):
        items = [item for item in inventory if item.source_url == source_url]
        headings = [item for item in items if item.kind == "heading"]
        contacts = [
            item for item in items if item.kind == "contact" and _href(item.original_url or "")
        ]
        navigation = [
            item for item in items if item.kind == "navigation" and item.original_url in paths
        ]
        actions = contacts or navigation
        primary = actions[0] if actions else None
        proposals.append(
            {
                "source_url": source_url,
                "destination": paths.get(source_url),
                "status": "proposal_requires_business_review",
                "heading_structure": [
                    {"label": item.text, "source_item_id": item.id} for item in headings
                ],
                "primary_action": {
                    "label": primary.text,
                    "target": paths.get(primary.original_url or "", primary.original_url),
                    "source_item_id": primary.id,
                    "rationale": "Point de contact existant"
                    if contacts
                    else "Page existante du parcours",
                }
                if primary
                else None,
                "content_source_ids": [item.id for item in items if item.kind == "text"],
                "needs_confirmation": ["Choisir l'action principale selon l'objectif commercial."]
                if primary
                else [
                    "Définir une action principale : aucun contact ou lien interne exploitable observé."
                ],
            }
        )
    return proposals


def _stylesheet(palette: BrandPalette, direction: BrandDirection | None = None) -> str:
    fonts = {
        "humanist": "system-ui,sans-serif",
        "editorial": "Georgia,serif",
        "technical": "ui-monospace,monospace",
    }
    font = fonts[direction.typography] if direction else fonts["humanist"]
    measure = {"editorial": "55rem", "studio": "64rem", "catalog": "72rem"}
    width = measure[direction.layout] if direction else "55rem"
    rhythm = "2.1rem" if direction is None or direction.density == "spacious" else "1.3rem"
    return f""":root{{--bg:{palette.background};--surface:{palette.surface};--ink:{palette.text};--muted:{palette.muted};--accent:{palette.accent};--on-accent:{palette.accent_text};font:100%/1.65 {font};color:var(--ink);background:var(--bg)}}
*{{box-sizing:border-box}}body{{margin:0}}header,main,footer{{max-width:76rem;margin:auto;padding:clamp(1rem,4vw,3.4rem)}}
header{{border-bottom:1px solid var(--muted)}}nav{{display:flex;flex-wrap:wrap;gap:.8rem 1.3rem}}a{{color:var(--accent);text-underline-offset:.2em;overflow-wrap:anywhere}}
nav a{{min-height:2.75rem;display:inline-flex;align-items:center}}[aria-current=page]{{font-weight:700}}main{{max-width:{width}}}h1{{font-size:clamp(2rem,5vw,3.4rem);line-height:1.2;letter-spacing:-.025em}}h2{{font-size:clamp(1.3rem,3vw,2.1rem);line-height:1.3;margin-top:{rhythm}}}p{{max-width:70ch;white-space:pre-wrap}}img{{display:block;max-width:100%;height:auto}}figure{{margin:{rhythm} 0}}details,aside{{margin-top:{rhythm};padding:1.3rem;background:var(--surface);border:1px solid var(--muted)}}summary{{cursor:pointer;min-height:2.75rem}}.transcript{{white-space:pre-wrap;overflow-wrap:anywhere}}.source-note,footer{{color:var(--muted)}}:focus-visible{{outline:3px solid var(--accent);outline-offset:4px}}.skip{{position:absolute;left:-9999px}}.skip:focus{{left:1rem;top:1rem;background:var(--surface);padding:1rem}}@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto}}}}"""
