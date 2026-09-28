# Website rendering and binary evidence adapters

These adapters supplement `website_dossier.py`. Its original HTML dossier and
literal `rendering=not_available` remain source-HTML evidence. A separate
`RenderedPage` records actual JavaScript-enabled Chromium execution. Neither
record proves site ownership, source claims, exhaustive discovery, or a completed
interactive customer journey.

## Application interfaces

```python
from pathlib import Path
from app.services.website_assets import (
    AssetReference, download_website_assets, discover_asset_references,
)
from app.services.website_browser import (
    BrowserRuntimeConfig, browser_runtime_capability, capture_rendered_page,
    extract_rendered_inventory,
)

config = BrowserRuntimeConfig(enabled=True)
capability = await browser_runtime_capability(config)  # real launch, no website request
rendered = await capture_rendered_page(
    "https://example.org/", output_dir=Path("/private/evidence/capture-123"), config=config,
)
additional_inventory = extract_rendered_inventory([rendered])
references = [reference for view in rendered.viewports for reference in view.asset_references]
assets = download_website_assets(
    references, source_url="https://example.org/",
    output_dir=Path("/private/evidence/capture-123/assets"),
)
```

The orchestrator supplies trusted storage directories, starts bounded jobs, and
stores these records with its revision. `fetcher=` is a trusted test/integration
seam, never an API input. By default it uses the existing `PublicHttpFetcher`.
No model, upload, publication, credentials, or source-site form submission occurs.

`extract_rendered_inventory` verifies the saved DOM byte count and SHA256 before
extracting additional source text/links. Its locators begin `rendered_dom:desktop:`
or `rendered_dom:mobile:` and the page digest belongs to that DOM observation.
The helper does not change the HTML dossier, mark new pages visited, or convert
source statements into verified facts. Retain the original HTML dossier and
rendering receipt when adding these entries to a reconstruction inventory.

## Browser capability and isolation

Install the optional Playwright runtime and a compatible Chromium as an explicit
operator provisioning step. The adapter never downloads a browser, opens a user
profile, or attaches to an existing browser. `enabled=False` is the default.
`chromium_executable` is a trusted deployment setting, not a request parameter.

Production requires Linux, a non-root service user, util-linux `unshare`, and
permitted unprivileged user/network namespaces. A probe must observe a different
`/proc/self/ns/net`; Chromium then starts through the same
`unshare --user --map-current-user --net` wrapper with its Chromium sandbox enabled.
The new network namespace has no configured external interface. Chromium's
Playwright control pipe still works; the Python coordinator remains outside the
namespace and supplies permitted responses. This prevents WebRTC, browser
background requests, speculative DNS, and unhandled browser transports from
reaching host/private/public networks directly. Run this capability in a worker
with OS memory/CPU/process limits appropriate to untrusted JavaScript; the adapter
does not provision a container or cgroup.

Missing Playwright, missing Chromium, denied namespaces, root execution, or
non-Linux hosts return `unavailable` before website content is fetched. A configured
flag does not imply verified capability. No insecure proxy-only fallback exists.

All HTTP document/script/style/font/image/GET-fetch requests use the controlled
transport. The source hostname stays fixed, including redirects; an HTTP-to-HTTPS
upgrade is permitted but a downgrade from any current HTTPS source page/hop is not.
Every DNS answer must be public and the socket connects to the validated peer with
TLS/SNI checks. `robots.txt` is enforced for every resource and redirect target;
robots failure blocks acquisition. Cookies, credentials, server `Set-Cookie`,
refresh, preload and reporting headers are never forwarded. Non-GET requests,
additional navigations, frames, workers, service workers, WebSockets, SVG documents
and unrecognized response MIME types are blocked. CSP further limits protocols
and execution targets. The restricted render may therefore differ from a normal
browser session, especially for third-party CDNs, authentication, POST-backed
applications, workers, canvas/media, or cross-origin embeds.

Network budgets are shared across the two independent viewport contexts: defaults
100 requests including robots/redirects, 1 MiB per response, 8 MiB total bodies,
60 seconds including browser startup, and three redirects per resource. Each
viewport also bounds intercepted attempts so rejected or cached requests cannot
bypass its request count. A screenshot covers the viewport only, at 1280×900 and
390×844, with scale 1. Each screenshot records PNG bytes, dimensions, URL, source
HTML digest, DOM digest and capture time. DOM and screenshot are sequential
observations; JavaScript timers can change state between them. The mobile capture
is Chromium mobile emulation, not physical iPhone or Safari validation.

`captured` means both bounded viewport observations completed without recorded
errors, not that all site behavior was reproduced. Blocked/failed resources and
browser errors yield `partial`; absent runtime yields `unavailable`; failed
execution yields `failed`. No static HTML image is substituted for a browser result.

## Media and safe preview

`discover_asset_references` reads `img src`, `img/source srcset` HTTP(S) candidates
and PDF links, preserving source page/digest/locator. Discovery checks URL syntax,
rejects literal nonpublic IP addresses and localhost names, and reports truncation.
It does not resolve hostnames: a discovered hostname may still resolve to a private
address. Links remain unverified references until binary acquisition. Downloads
validate every DNS answer and pin the connection to the validated public peer,
including each redirect, under the same robots/request/time/byte policy. Only PNG,
JPEG, GIF, WebP and signature-checked PDF are accepted.

Original bytes are stored content-addressed with a `.bin` suffix and SHA256.
Pillow must successfully decode the declared raster format, verify its integrity,
and satisfy the pixel budget. A separate single-frame RGBA PNG preview is encoded
without source metadata. Reconstruction should use that preview's bytes, digest,
size and `image/png` MIME type. SVG and HTML are never accepted as preview assets.
Animated source images produce a first-frame preview, not animation evidence.

PDF validation checks the declared MIME and bounded file signature only. It is
not malware detection or PDF content extraction. PDFs have no inline preview and
must remain downloads (`application/octet-stream`, `Content-Disposition: attachment`,
`X-Content-Type-Options: nosniff`) if exposed at all. Never place source `.bin`,
DOM `.dom.bin`, or PDF bytes in an HTML iframe/object/embed. Serve only validated
preview PNGs inline from the evidence store.

Asset defaults: 30 attempted unique URLs, 8 million pixels, 8 MiB per encoded preview.
Every accepted record includes original/final asset URLs, source page URL/digest,
locator, fetch time, local paths, MIME types, byte counts and hashes. It retains
`disposition=unverified_source_asset`; a successful download is not a license grant
or a verified business claim. Callers must retain failures/limits instead of
hotlinking omitted assets or claiming complete coverage.

## Verification

`tests/test_website_runtime.py` serves a real benign HTTP fixture, downloads and
decodes real binary images, and checks robots, redirects, HTTPS downgrade, byte and
pixel limits, signatures, hashes, provenance, and symlink refusal. With
`SWARMER_TEST_CHROMIUM` set to an existing compatible Chromium executable, it also
executes external JavaScript and JSON fetch, checks changed DOM text and both PNG
dimensions, verifies rendered inventory, and probes blocked POST/private-network/
robots/WebSocket/worker/SVG paths. It never installs a browser.

On developer Macs only the test fixture replaces the Linux launcher boundary;
the browser, routing, JavaScript and local HTTP server remain real. Those tests
do not qualify production Linux namespaces, public TLS, third-party client sites,
or physical iPhone behavior. Production namespace availability is tested by the
runtime capability probe and must remain unavailable until that probe succeeds.

```sh
cd server
SWARMER_TEST_CHROMIUM=/path/to/existing/chromium python -m pytest tests/test_website_runtime.py tests/test_website_dossier.py -q
python -m mypy app/services/website_assets.py app/services/website_browser.py
python -m ruff check app/services/website_assets.py app/services/website_browser.py tests/test_website_runtime.py
```

Primary runtime references: [Playwright request interception and service-worker
limitations](https://playwright.dev/python/docs/network), [browser context WebSocket
routing](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket),
and [Linux unshare network namespaces](https://man7.org/linux/man-pages/man1/unshare.1.html).
