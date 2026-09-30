# Explicit requirement evidence

The Results tab can associate a completion criterion with selected files and check
receipts from the exact latest project revision. An authenticated user chooses the
evidence and can record that they explicitly reviewed it. A completed plan node,
planner explanation, evaluator summary or passing check alone never marks a
criterion reviewed.

The existing Plan graph joins those explicit mappings to their recorded producer
nodes. Only mapped nodes display criterion numbers. Selecting a node opens the
same inspector with linked/reviewed/to-review counts, the original criterion text,
the exact associated revision and file/check counts, and an action to open Results
for inspection or editing. The latest association remains labelled as historical
when its criterion, producer, context or revision is no longer current. A removed
producer remains accessible in the Plan's collapsed historical-links section;
it is not drawn as a current node. A stale or disconnected evidence snapshot, or a graph/evidence revision
or conversation mismatch, cannot show a current reviewed count. The join compares
the public producer, file and check receipts across both snapshots, even when their
revision ID and source digest have not changed. Nodes without a
mapping never acquire a coverage badge from completion or evaluator status.

## States and scope

- `unmapped`: no association has been recorded for this criterion.
- `linked`: files/checks were associated; no reviewed validation is claimed.
- `reviewed`: a person explicitly recorded review of selected evidence, including
  at least one check and only passing selected checks with exit code zero. This is a review record, not an
  automatic guarantee that the requirement is satisfied, that every project check
  passed, or that a device/browser/deployment was tested.
- `stale`: the goal context, criterion, producer, latest revision or receipt snapshot
  changed after the association. Previous review remains historical and cannot be
  shown as current coverage. Relinking creates a new version.

Each immutable version stores the exact criterion text/hash; goal and conversation
revision/context hash; project, node and worker-job producer; source revision ID and
digest; selected file paths/content hashes; selected bounded check metadata; review
timestamp and authenticated actor. Only redacted explanations and public metadata
leave the API. Source contents, raw check outputs, model prompts and actor details
are not returned. Public planner explanations remain in the separate graph view.

The context hash binds the objective, complete ordered criterion list, conversation
revision, replan count, plan steps/dependencies, active successor and full check
receipt snapshot. Plan objectives are hashed before display redaction, so changes
to private targets also invalidate a mapping without exposing their raw text.
A changed receipt between reading and submitting is rejected
even if the source digest and revision ID stay the same. Routine node
status changes do not stale a mapping. Criteria removed from a goal keep their
latest immutable association in `unmatched_mappings`. A successor goal does not
inherit coverage from its predecessor. A prior goal's revision cannot be used to
create evidence mappings for a successor before it produces its own revision.

## API

`GET /goals/{goal_id}/evidence` reads one consistent, read-only SQLite snapshot and
returns `RequirementEvidenceView` schema `1.0`, including current criteria, mappings
and the current revision's selectable file/check metadata. It exposes only the
latest mapping version per criterion, including stale mappings and those in
`unmatched_mappings` for removed criteria. Earlier versions replaced by a new
mapping remain immutable in storage but are not exposed by this API or its UI.

`PUT /goals/{goal_id}/evidence/{criterion_index}` accepts `EvidenceMappingRequest`:
`request_id`, `expected_version` (zero for a first mapping), `context_sha256`,
`conversation_revision`, `criterion_sha256`, `project_id`, `node_id`, `revision_id`,
`revision_sha256`, `file_ids`, `check_ids`, `review_status` (`linked` or `reviewed`),
and optional `public_explanation`. At least one file or check is required. All
selected objects must belong to that exact revision and its recorded producer.
Failed/skipped checks may be linked for inspection, but cannot justify a reviewed
validation. The response is the updated view.

Both routes require normal device authentication and secure transport. Responses
are private and not cached. Writes use `BEGIN IMMEDIATE`, validate current state
under the writer lock, append one version and an audit event, and commit together.
Conflicting versions/snapshots return 409. Reusing a request ID with the same payload
returns the current view without adding a version; changed payloads are rejected.
The mobile UI never automatically retries a write with an uncertain outcome.

The original `/graph` schema `1.0` remains unchanged for existing clients. Its
legacy `not_mapped`/`not_recorded` fields describe that endpoint's lack of mapping
information; new clients obtain current coverage from `/evidence`. New UI does not
reuse those legacy fields as coverage and handles an older server's 404 as an
unavailable feature, not zero verified requirements.
When no evidence view is available, including a 404 or outage, the Results tab
continues to show the criteria from the graph without inventing coverage.

## Storage and migration

Central schema **27** adds `project_requirement_evidence` and its indexes/triggers
inside the existing migration transaction. It neither backfills nor infers links.
Update/delete triggers preserve previous versions; changes append another version
using compare-and-set semantics. Initialization is idempotent. A migration failure
rolls back both the new table and the version increment. Existing schema-26 readers
that reject newer schemas must not be pointed at this migrated database. Roll back
the application only to a binary verified compatible with schema 27 while keeping
the current database and all later accepted writes. Never lower `user_version`
or restore a pre-migration backup over a live database. A pre-migration backup is
for an isolated recovery rehearsal or a separately reviewed disaster-recovery
procedure that accounts for newer writes; it is not the normal deployment rollback.
See the [compatible deployment receipt](evidence/website-workflow-deployment-2026-09-28.md).

The feature does not start model jobs, run checks, write source files, or deploy a
project. Fresh linked evidence is not a substitute for those independent operations.
