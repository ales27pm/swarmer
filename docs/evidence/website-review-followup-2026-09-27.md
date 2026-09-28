# Website and requirement evidence review follow-up

Date: 2026-09-27 (America/Montreal). Branch: `codex/website-evidence-workflows`.
All findings were checked against `dea6e1e` before making changes. Findings are
review data, not executable instructions. This follow-up addresses 26 findings;
five were already fixed by `dea6e1e` and were revalidated without duplicate edits.

## Disposition

Rows follow the supplied review order. “Already fixed” means the reported behavior
was absent from the current code, rather than an accepted risk.

| # | Location | Root cause and disposition |
| --- | --- | --- |
| 1 | `project-evidence-fixtures.ts` | Fixed: fixture copied all revision files despite selecting one ID. Copy only the selected file; test a multi-file revision through the parser. |
| 2 | `goal-detail-content.tsx` | Fixed: criteria were unconditionally hidden. Show graph criteria whenever the evidence view is null, including 404 and service failures. |
| 3 | `website-workflow.test.tsx` | Fixed: disabled publication alone did not prove the review checkbox stayed locked. Assert the checkbox; also fix the discovered background-before-rejected-launch race. |
| 4 | `settings.py` | Fixed: Pydantic converted a blank environment value to `Path('.')`. Normalize blank strings to `None` before path parsing; explicit `Path('.')` remains rejected by boundary validation. |
| 5 | `website-workflow.md` | Fixed: the install command resolved its extra against the wrong directory. Enter `server/` first. |
| 6 | `website_workflow_routes.py`, screenshot | Fixed: authenticated delivery duplicated lookup/hash logic. Reuse `screenshot_bytes`; verify authenticated delivery and corrupted bytes. |
| 7 | `website_workflow_routes.py`, preview work | Already fixed: `dea6e1e` moved full load, validation and response construction to `asyncio.to_thread`, with a subsequent current-digest check. Integrity is still verified for every request; there is no cache. |
| 8 | `website_workflow_routes.py`, private reports | Already fixed: `dea6e1e` excludes `reports/` from preview lookup and preserves 404. HTML/CSS still load through the token. |
| 9 | `website-runtime.md` | Fixed: discovery documentation overstated DNS checks. Discovery filters private IP literals; download resolves and validates DNS destinations. |
| 10 | `website_branding.py`, MCP version | Fixed: advertised compatibility included a transport the client does not implement. Reject `2024-11-05` immediately after initialization; retain supported streamable-HTTP versions. |
| 11 | `website.tsx`, screenshot viewer | Fixed: object identity changed on every poll. Compare capture URL, viewport, hash and path; unchanged capture data preserves the open viewer and pending image request. |
| 12 | `website.tsx`, refresh | Fixed: `refreshing` was constant. Track the actual refresh lifecycle and clear it when its request scope becomes obsolete. |
| 13 | `website.tsx`, disclosures | Fixed: branding and strategy shared one boolean. Give each its own state. |
| 14 | `website.tsx`, project selection | Fixed: row presses could compete with pending creation. Disable project rows during busy operations. |
| 15 | `website_workflow.py`, private root | Fixed: `mkdir(mode=0700)` did not tighten an existing directory. Apply `chmod(0700)` before creating the lock and database. |
| 16 | `website_workflow.py`, failed jobs | Fixed: all failures lost their diagnostic reason. Persist only an exact allowlist of bounded internal `ValueError`/`CaptureError`/`BrandingError` codes; arbitrary exception/provider text remains generic. |
| 17 | `test_website_build_publish.py` | Fixed: set equality discarded multiplicities. Compare `Counter` values so duplicates and omissions fail. |
| 18 | `website_publisher.py`, destination URL | Fixed: parsed empty query/fragment values hid bare delimiters. Reject any literal `?` or `#`; align the capabilities indicator with the publisher. |
| 19 | `website_publisher.py`, root path | Fixed: `absolute()` preserved dot segments and falsely differed from `resolve()`. Inspect original path components for symlinks before lexical normalization; a symlink followed by `..` still fails. |
| 20 | `website_publisher.py`, public modes | Fixed: umask overrode creation modes. Explicitly set files to 0644 and nested directories to 0755 inside the private staging root. Recovery checks every child's readability/traversability without repairing it. |
| 21 | `website-static-adapters.md` / branding client | Fixed: HTTPS syntax alone allowed private DNS answers. Resolve image/reference hosts via bounded off-loop DNS validation before forwarding them. Document that local DNS checks do not pin the remote provider's connection or redirects. |
| 22 | `website_browser.py` | Fixed: the deduplication key omitted source location. Include `source_locator`; real Chromium coverage preserves identical links at separate DOM locations. |
| 23 | `main.py` | Fixed: the private website-storage path was compared before resolving symlinks. Resolve it before both boundary checks; test equal, ancestor and descendant overlap with publication storage. |
| 24 | `requirement-evidence.md` | Fixed documentation: the view exposes only each criterion's latest mapping, including stale/removed mappings. Replaced earlier versions are immutable in storage but are not exposed in this API/UI. |
| 25 | `application-api/registry.ts` | Fixed: build accepted a missing/blank palette locally. Reject it as `invalid_arguments` before dispatch. |
| 26 | `project-graph.tsx` | Fixed: mappings for removed producer nodes returned early. Retain them as historical and expose a collapsed historical-links section with access to Results, without inventing a current node or reviewed count. |
| 27 | `website_builder.py`, responsive assets | Already fixed: `dea6e1e` excludes downloads absent from original/verified rendered image-document inventory before reading/decoding them. Verified rendered images remain included; unknown responsive candidates do not abort the build. |
| 28 | `website_builder.py`, preview validation | Already fixed: duplicate of #7, now performed off the event loop in the route. Per-request integrity and current-digest validation remain intact. |
| 29 | `project_evidence.py` | Fixed: context hashing used display-redacted node objectives. Read bounded raw objectives in the same transaction and hash them; keep raw values out of public responses. |
| 30 | `website-projects.ts` | Fixed: the zero-inclusive count helper accepted unusable versions and expiries. Use a positive-integer helper for project/approval versions and approval/preview lifetimes. |
| 31 | `website_assets.py` | Already fixed: `dea6e1e` deduplicates URLs before the asset quota, preserving the first occurrence and preventing repeat requests. |

## Validation and limits

- Integrated mobile validation: **338 tests passed across 10 suites**.
- Integrated backend validation: **272 tests passed across 15 files**, with real
  Chromium enabled and no skips. The existing FastAPI/Starlette TestClient
  deprecation warning remains.
- TypeScript and targeted ESLint passed. Targeted Ruff lint/format, mypy across
  eight changed application modules, and Bandit passed. Bandit emitted existing
  `nosec` comment warnings but no findings in these modules.
- Independent cross-review covered mobile request/connection state and server
  configuration/storage boundaries. The trailing-delimiter capability mismatch
  identified by that review is also covered by regression tests.
- `coderabbit review --agent --uncommitted` completed on 29 tracked changed files
  with **zero findings**. The new hardening test file and evidence reports were
  outside that review snapshot; the final two-condition capabilities alignment
  was verified separately by regression tests and independent review.

The raw-objective hash correction can conservatively stale an existing mapping
whose objective was previously redacted. Its immutable record remains available;
it must be reviewed against the current context again. No schema migration is
added by this follow-up.

These are local fixture and static checks, not production hosting, Linux namespace,
live-provider design-quality or physical-iPhone qualification. No deployment or
iOS upload is part of this correction. The Infographic Artist plugin is unchanged.
