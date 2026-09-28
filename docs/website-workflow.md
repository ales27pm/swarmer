# Native website workflow

Open **Projets → Sites web et identité visuelle** in the mobile app. Create a
project from the company's existing public URL and a business objective. The
flow is capture → brand research → explicit layout/palette selection → private
preview → exact-version publication review. Projects can be resumed from the
same screen. Draft input survives a pairing change; old server results and
publication confirmations do not.

## What is implemented

- Authenticated `/website-projects` API and durable device-owned project records.
- Bounded source HTML crawl, page inventory and provenance. Original source
  data is preserved; commercial claims are not treated as verified facts.
- Optional isolated Chromium renders, desktop/mobile screenshots, verified DOM
  inventory, downloaded raster media and document attachments.
- Native Infographic Artist MCP HTTP discovery, design-system research and
  direction generation. The endpoint is configured by the server operator;
  a Codex connector does not automatically provide credentials to Swarmer.
- Multipage static reconstruction populated from the actual captured content.
  Three explicit palettes and three composition choices affect the output.
  These presets are user choices; provider recommendations are retained as
  referenced evidence, not falsely attributed as generated layouts.
- Source-specific marketing proposals, old/new URL mapping, per-item migration
  dispositions, contrast checks, and unresolved content/form/media issues.
- Expiring private preview links and screenshot viewers. Source JavaScript and
  forms are never executed in the rebuilt site or preview.
- Publication of the exact reviewed public files to an operator-configured
  static hosting directory. Private migration/strategy/readiness reports remain
  in authenticated storage and are excluded from deployment.

This is a static-site workflow. It does not silently recreate transactional
forms, checkout, authentication, CMS editing or third-party application backends.
Observed forms remain visible as work to configure. It does not send marketing
campaigns, change DNS, modify the Infographic Artist plugin or deploy itself.

## Runtime configuration

Install the optional runtime in the server environment:

```sh
cd server
python -m pip install -e '.[website]'
python -m playwright install chromium
```

The following settings use the existing `MONGARS_` prefix:

| Setting | Default / purpose |
| --- | --- |
| `WEBSITE_BROWSER_ENABLED` | `false`; enable isolated rendering |
| `WEBSITE_CHROMIUM_EXECUTABLE` | optional Chromium executable path |
| `WEBSITE_BROWSER_MAX_PAGES` | `3`, at most `30`; explicit render sampling bound |
| `INFOGRAPHIC_ARTIST_ENDPOINT` | MCP HTTP endpoint; unset means unavailable |
| `INFOGRAPHIC_ARTIST_TOKEN` | optional operator-provisioned secret; never returned to clients |
| `WEBSITE_PUBLISH_ROOT` | existing dedicated directory served by the configured host |
| `WEBSITE_PUBLIC_BASE_URL` | HTTPS base URL corresponding exactly to that directory |
| `WEBSITE_ATTACHMENT_HEADERS_CONFIGURED` | `false`; enable only after configuring attachment/no-sniff headers for `.bin` documents |

Production rendering requires Linux, a non-root service account and usable
unprivileged network/user namespaces. There is no non-isolated fallback.
`browser_runtime_capability(config)` verifies the actual browser launch.
Configuration alone is not runtime proof. See [website-runtime.md](website-runtime.md)
for network limits and tested browser behavior.

Storage lives beside the main database in `<database-stem>-website-projects/`,
outside the agent-writable workspace. It contains its own SQLite database,
immutable capture directories and hashed build records. One process owns that
directory using an exclusive lifetime lock. Back up this directory together
with the primary database; it is not a public document root.

For the initial static hosting adapter, configure an HTTPS file server serving
only `WEBSITE_PUBLISH_ROOT`. Give the Swarmer service account write access there.
The root must not overlap the agent workspace or private state. For document
downloads, the web server must return `Content-Disposition: attachment` and
`X-Content-Type-Options: nosniff` for `.bin` files before declaring attachment
headers configured. A local copy receipt does not establish DNS/HTTP availability.
Vercel/Netlify API deployment is not implemented by this adapter.

## API and durable operation semantics

All project routes use the existing paired-device HTTPS authentication. Project
read/write ownership is device-scoped. Capture and build requests carry a stable
`request_id` and `expected_version`; duplicate or stale mutations cannot start
a second operation. At most two jobs execute concurrently with a bounded queue.
Long operations persist their phase before work starts. Interrupted capture,
branding and build jobs are marked as such and require an explicit retry.

| Route | Purpose |
| --- | --- |
| `GET /website-projects/capabilities` | Configuration flags and palettes |
| `GET/POST /website-projects` | Resume or create projects |
| `GET /website-projects/{id}` | Current state, provenance summary and results |
| `POST /website-projects/{id}/commands` | `capture`, `branding`, or `build` |
| `GET /website-projects/{id}/dossier` | Original full source inventory |
| `GET /website-projects/{id}/screenshots/{sha256}` | Authenticated verified PNG |
| `POST /website-projects/{id}/screenshots/{sha256}/preview` | Five-minute read-only image link |
| `POST /website-projects/{id}/preview` | Five-minute read-only reviewed build link |
| `POST /website-projects/{id}/publication-review` | Preflight and version/destination-bound approval |
| `POST /website-projects/{id}/publish` | Consume explicit one-use publication approval |

Preview links grant read access only to the exact project artifact. They carry
no paired-device credential, expire after five minutes and become invalid when
the corresponding artifact is replaced. HTML previews have a restrictive CSP,
no scripts or forms, no-referrer and no-store. Native confirmations are cleared
on backgrounding, connection changes and changed project revisions.

Publication runs as an owned task even if the initiating HTTP request is
cancelled. Shutdown drains it before releasing the service lock. Its durable
intent binds the approved build, resolved hosting root and public destination.
After a crash, recovery only reads and verifies an existing exact release;
it never publishes automatically. A changed destination, absent release or
modified file leaves the operation interrupted without a fabricated receipt.
Previous releases are preserved. See [website-static-adapters.md](website-static-adapters.md).

## Verification and remaining environment qualification

The test suites exercise authenticated API capture/build/preview/publication,
real HTTP MCP fixtures, real Chromium fixture rendering, media checks,
cancelled-request publication, process locking, crash recovery, migration
accounting, private-report exclusion and mobile connection/approval behavior.

No real company URL, live Infographic Artist endpoint or hosting destination
was supplied for this implementation. Linux namespace execution and the
physical iPhone build require qualification in those environments. A new iOS
build is required for the native screen; backend configuration alone does not
install it on an existing phone build.

The separate [requirement evidence feature](requirement-evidence.md) operates on
central goal/project revisions and recorded validation receipts. It does not
pretend these independent website artifacts are central worker revisions.
