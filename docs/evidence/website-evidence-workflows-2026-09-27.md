# Requirement evidence and native website workflow — implementation evidence

Date: 2026-09-27 (America/Montreal). Implementation branch:
`codex/website-evidence-workflows`, based on `a5bf3b5`.

## Delivered behavior

The project graph now shows explicit requirement associations on the nodes that
produced their selected revision, files and validation receipts. Results provides
the authenticated mapping/review interface. Changed requirements, conversation
context, producers, revisions or receipts invalidate current review coverage;
the latest stale or removed association remains historical. Earlier replaced
versions remain in storage but are not exposed in the API or UI. Completion alone
never claims validation.
This adds transactional central database schema 27; rollback requires the
pre-migration backup rather than lowering the schema version.

The native mobile entry is **Projets → Sites web et identité visuelle**. It supports
creating and resuming a website project from an existing company URL and objective,
bounded source discovery, optional isolated browser rendering and screenshots,
downloaded media, native Infographic Artist consultation, explicit palette and
composition selection, source-populated static reconstruction, private previews,
and exact-version publication confirmation.

The builder preserves source provenance and generates migration, strategy and
readiness reports. Incomplete capture, unavailable media, observed forms and
unverified commercial claims stay visible. Existing source text supplies the
pages; commercial facts are not invented. Infographic Artist presets are not
fabricated: a missing configured service is unavailable, while chosen layout
presets are attributed to the user. The original plugin is unchanged.

Publication installs immutable public files in a configured static hosting
directory. Private reports are excluded. The approval binds the build digest,
project version and destination. Cancellation and restart recovery cannot silently
publish a different revision; recovery only verifies an existing exact release.
Private previews and screenshot links expire and contain no device credential.

## Verification

Initial implementation scoped results (see the [review follow-up](website-review-followup-2026-09-27.md)
for subsequent fixes and fresh validation):

- **194 backend tests passed** across 13 files: requirement evidence, graph,
  migrations (including v0.10/v0.12), website dossier/runtime/branding/build/
  publication/workflow, API resources and contract schemas. This includes all
  17 browser/media tests with real Chromium enabled and 55 dossier tests with
  XML DTD/entity/encoding regressions. No test in this run was skipped.
- **318 mobile tests passed** across 10 suites: graph/requirement evidence,
  goal details, website screen, API clients and application command registry.
  Regression cases include connection changes, outdated GET responses racing
  writes, replaced screenshots, changed approval versions and divergent graph
  receipt snapshots sharing the same revision ID/digest.
- TypeScript checking and targeted ESLint passed. Ruff lint/format passed;
  strict mypy passed for 17 touched application modules. Bandit reported no
  findings in the 14 new/relevant evidence and website modules.
- The repository-wide Bandit scan still reports the pre-existing B608 in
  `app/services/swift_project_validation.py:334`; the identical dynamic SQL
  expression is present in base commit `a5bf3b5`. That unrelated file was not
  changed. The run is not represented as a globally clean security scan.
- `git diff --check` passed. The temporary mobile dependency symlink was removed.

Tests ran in the isolated implementation checkout, using Node 24.19.0 and Python
3.12 with the optional Playwright/Pillow runtime. Real Chromium fixture execution
used an existing Chromium executable; it did not install one or publish a real
site. HTTP fixture tests exercised the MCP protocol without contacting a live
Infographic Artist account. Pytest reported one existing FastAPI/Starlette
deprecation warning concerning the TestClient HTTP dependency.

## Environment qualification still required

- Configure the real Infographic Artist MCP endpoint and any operator-managed
  credential. HTTP fixture tests establish client behavior, not live provider
  availability or response quality.
- Provision Chromium and verify the Linux non-root network-namespace capability.
  Mac fixture tests exercise real Chromium through a test launcher; they do not
  qualify Linux isolation or Safari/iPhone rendering.
- Configure a dedicated public hosting directory and its HTTPS base URL. If
  document attachments are published, configure attachment/no-sniff headers.
  Vercel and Netlify API adapters are not included. A local publication receipt
  is not a live HTTP/DNS check.
- Exercise a real company URL and review capture coverage, business content,
  visuals, accessibility and interactive integrations. This is a static-site
  reconstruction; transactional forms, commerce, authentication and CMS backends
  require their own integrations.
- Deploy the backend with a database backup and build/install the updated iOS
  application. No production deployment, TestFlight upload or physical-device
  validation of this branch was performed during implementation.

The other release thread's existing backend/iOS releases do not contain this
branch. Source was kept isolated from its release work and unrelated dirty files.

## Implementation references

- [Requirement associations and API](../requirement-evidence.md)
- [Native flow, configuration and recovery](../website-workflow.md)
- [Browser and media boundaries](../website-runtime.md)
- [Infographic Artist, builder and static publisher](../website-static-adapters.md)
