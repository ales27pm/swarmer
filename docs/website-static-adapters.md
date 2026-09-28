# Website branding, reconstruction and static publication

These adapters are part of the native website workflow. They do not change the
separately frozen iOS release or publish anything during tests.

## Infographic Artist

`InfographicArtistClient(endpoint, bearer_token=None).call(tool_name, arguments)`
uses real MCP HTTP requests: initialize, initialized notification, tool discovery,
then a supported tool call. JSON and SSE replies, server session IDs and protocol
versions are supported. Requests have a wall deadline, a 1 MiB response limit,
bounded catalog pagination, no redirects and no environment proxies. Errors use
stable codes; third-party response text and credentials are never copied to them.

Supported tools and their known arguments are:

- `search_design_systems`: query, kind, limit.
- `generate_brand_directions`: name, promise, sector, audience, traits,
  must_avoid, risk_tolerance.
- `critique_brand_image`: image, reference, context.

The native client checks the provider's advertised schema before each call. The
ChatGPT connector's local-file upload bridge is **not** assumed to exist here.
Critique is unavailable unless the provider explicitly advertises URI image
inputs; those inputs must be public HTTPS media references supplied by the caller.
Local server paths are never sent as remote images. Merely configuring an endpoint
does not upload screenshots or publish them. A missing provider returns
`infographic_artist_not_configured`, never a fabricated brand direction.

## Reconstruction contract

`WebsiteBuilder.build(dossier, *, palette, assets=None, brand_brief=None,
direction=None, verified_rendered_inventory=None, business_objective=None)` returns
a serializable `WebsiteBuild`. `WebsiteBuild.model_json_schema()` exposes the
schema. Files have a relative path, media type, size, SHA-256, and exactly one of
UTF-8 text or base64 bytes. The total output is bounded to 40 MiB.

The source dossier remains unchanged. Original inventory and independently
verified DOM inventory are distinguished in the migration report. Every source
item receives `retained`, `needs_confirmation` or `excluded` with a specific
reason and destination. Missing media, uncaptured targets, incomplete extraction,
source forms and insufficient palette contrast stay visible as blockers. Source
HTML and JavaScript are not copied into the reconstruction. Text, headings, link
labels and metadata are escaped, links are mapped to generated pages, and forms
are represented by an explicit configuration notice.

Select a `BrandPalette` explicitly. `BrandDirection` offers validated layout,
typography and density values that change CSS and composition. If the direction
is attributed to Infographic Artist, `source_result_sha256` must equal the SHA-256
of `canonical_json(brand_brief)`, where the brief is the successful native-call
envelope retained by the workflow. A human-selected projection of the provider's
direction into those choices is reviewable; arbitrary provider text never becomes
HTML, CSS, code or an asserted company fact.

Raster assets must be the browser/media collector's re-encoded PNG previews,
with their own verified SHA-256. PDF originals are downloadable `.bin` files with
`application/octet-stream`; preview and hosting must return attachment disposition
and `X-Content-Type-Options: nosniff`. SVG, source HTML and other active assets are
not accepted. No source images are silently hotlinked.

Three hashed reports accompany the static site:

- `reports/migration.json`: all inventory dispositions, old/new page paths,
  source metadata, original structured data and observed form definitions.
- `reports/strategy.json`: source claims and provenance, user business objective,
  proposed page structure and primary action drawn from actual links, native
  branding result and selected direction. Unknown audience, differentiators,
  performance targets and marketing budget remain explicitly unconfirmed.
- `reports/readiness.json`: source gaps, automated checks, contrast measurements,
  and human visual/accessibility/business checks that remain unverified.

## Publication contract

`StaticDirectoryPublisher(root, public_base_url,
attachment_headers_configured=False).publish(build, expected_digest=...,
release_id=...)` writes the **same reviewed public site bytes** to a new immutable
release directory. Internal `reports/*` (business objectives, provider briefs,
source inventory and review evidence) remain private and are not published.
The public manifest contains only the reviewed build digest and public file
paths, types, sizes and hashes. The receipt binds both `reviewed_build_digest`
(also retained as `digest`) and `deployed_digest`, and reports how many private
reports were excluded. It does not crawl again, regenerate the site, change DNS or
replace a previous release. The caller owns authorization, one-use approval,
project/revision association and persistence of the returned receipt.

Both the existing local hosting root and HTTPS public URL are administrator
configuration, not request-controlled destinations. The adapter snapshots and
verifies all bytes/reports, rejects path traversal and symlink roots, uses
descriptor-relative no-follow writes, serializes publishers with a nonblocking
file lock, and atomically renames its private staging directory. Reusing a release
is rejected. Publication containing attachments is blocked until the hosting
header configuration is explicitly declared. A receipt records the URL and exact
digest; HTTP reachability or DNS configuration is not inferred from a file write.

`recover(build, expected_digest=..., release_id=...)` reconciles a process crash
after the atomic rename. It returns the same receipt only after a read-only walk
verifies the exact public file set, every deployed file's bytes and the safe public manifest, without following
symlinks. Missing releases return `None`; changed, injected or symlinked files fail
verification. Normal publication still rejects replay.

## Local evidence

Run from `server` with the environment's Python and `PYTHONPATH=.`:

```
python -m pytest -q tests/test_website_branding.py tests/test_website_build_publish.py
ruff check app/services/website_branding.py app/services/website_builder.py app/services/website_publisher.py
mypy app/services/website_branding.py app/services/website_builder.py app/services/website_publisher.py --follow-imports=silent
```

The MCP tests use a real HTTP fixture and cover JSON/SSE, session propagation,
schema drift, provider errors, missing configuration and unsupported image
transport. Build/publication tests cover source retention, rendered content,
strict brand provenance, escaped markup, inert forms, missing assets, deterministic
digests, exact publication bytes, immutable previous releases, mutation, replay,
traversal, symlinks, contention and attachment policy. Live Infographic Artist and
public hosting remain separate environment checks.
