# monGARS navigation and observable activity — 24 September 2026

## Delivered behavior

The app has four primary destinations: Assistant, Activité, Équipe and Réglages. Existing deep links remain valid. Authorizations are reached from Activité; the agent catalog and connected agents from Équipe; local inference, semantic memory, phone capabilities and advanced diagnostics from Réglages. Secondary destinations expose a return action to their owning section.

Native navigator headers own the safe top area. Shared screen content is centered and bounded on larger viewports. Main pages no longer repeat their navigation title. Long objectives have explicit expansion; shorter multiline objectives are never silently truncated. The welcome and empty activity illustrations are bundled locally; their exact generation prompts are recorded in [the asset note](ui-assets-2026-09-24.md).

Primary task filters stay visible while the remaining statuses are grouped under “Autres filtres”. Creating a project is a separate disclosure and remains distinct from starting it. Settings group existing controls without changing pairing, authentication, capability grants or one-use approval rules. The interface does not claim zero agents or zero active projects when the server has not been checked successfully. A cached view is identified as such.

The web preview exposed an existing call to an unavailable `Keyboard.metrics` method. It is now optional; native keyboard measurements, input preservation and stale-measurement fences remain tested. Network failures have a useful French explanation with the original error behind a disclosure. The error itself remains an accessible alert, independently from the detail button.

## Detailed operations contract

`GET /tasks/{id}/activity` and `GET /goals/{id}/activity` return a versioned, authenticated, read-only projection of persisted model calls, worker jobs, tool calls, project revisions and check receipts. The default page is 50 rows, maximum 100. Cursors are scoped and opaque; stale cursors are rejected. Unknown timestamps and durations stay null. Check receipts are identified as receipts and are not represented as fresh execution events.

Only allowlisted metadata is exposed: operation kind/status, agent, role/model, tool, recorded timestamps/duration, revision/file count, known check commands and exit codes. Raw prompts, source text, private tool arguments, outputs and audit payloads are excluded. The projection uses a read-only SQLite connection and performs no business reconciliation. Normal authentication can still update device last-seen metadata.

The mobile API registry exposes the same contract as `tasks.activity` and `goals.activity`. A strict parser precedes display. The panel is loaded on demand, refreshes on foreground/live-sync notifications, deduplicates rows and invalidates reads on a scope/connection change. Background, closed-panel and disabled reads cannot commit a stale response. A changed cursor requires explicit refresh; unavailable older servers show an availability message. Existing evidence remains visible but marked stale after a network failure.

This is a **snapshot of recorded operations**, not a token stream or a complete trace of internal reasoning. File contents and filenames, arbitrary command output and unrecorded internal operations are not newly exposed. The interface makes this coverage explicit.

## Evaluator correction

The provider now rejects contradictory terminal replies: `done` with unresolved structured diagnostics, and `failed` without structured diagnostics. There are no free retries or invented diagnoses. Details, primary research and the semantic limitations are in [the evaluator note](../research/evaluator-evidence-grounding-2026-09-24.md). A well-formed answer can still be semantically wrong; the held-out model failures remain qualification failures. No new model is activated or removed by this interface work.

## Verification and release boundaries

- Independent review caught and fixed a short multiline-title accessibility defect. Regression tests preserve keyboard behavior and error announcements.
- All 873 React Native tests passed across 56 suites; TypeScript and ESLint also passed. Behavior tests cover the navigation, drafts/permissions, API parsing, pagination, reconnection, stale responses, history isolation and all existing model/capability flows.
- Browser validation used the real Expo web app at 393 × 852: welcome asset, four tabs, settings grouping, filters, unpaired/error states and return navigation. It did not pair the browser with production or create a user project.
- iOS Hermes export succeeded with bundled illustration assets. JavaScript export is distinct from a signed native archive and physical-device validation.
- The isolated backend backport contains only four runtime files; workers, dependencies, database schema 26 and model configuration are unchanged. Its tests passed 254 cases with two preexisting wire-schema failures reproduced against the predecessor. The activity smoke projected 20 existing goals and 20 tasks through a read-only connection without errors.
- Fallow's change audit still reports inherited dead-code findings and estimated complexity. Two duplicated settings rows were factored out; goal phase selection, budgets and task loading were separated into helpers. The activity parser intentionally retains domain-specific bounds and the concurrency/session guards remain explicit. No broad auto-fix, suppression or dependency deletion was applied to make the report look clean.

Deployment, signing and TestFlight availability must be confirmed by their separate release receipts. Inactive staging alone does not mean the server has changed. Device generation/background behavior is not newly qualified by this UI refactor.
