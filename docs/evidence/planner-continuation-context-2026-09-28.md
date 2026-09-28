# Preserve intent when continuing an existing project

## Observed failure

A user continued an existing website project. The latest `Continue` message was
stored correctly, but the planner's conversation card was truncated to 1,000
characters before that message. The linked saved revision was also absent from
the planner's context. Although `code.build_project` was advertised, the planner
assigned HTML, CSS and JavaScript implementation to three `writing.draft` nodes.
The prose-only writer declared `declined`; the server correctly retained that
outcome instead of claiming implementation success. This fix does not reinterpret
that historical response or change refusal handling.

## Change

The planner now receives a separate protected latest-user card and a protected
saved-project card. The latter reads the linked revision across goal continuations:
revision identity, runtime, file count and bounded untrusted file names. It reads
no source bodies or check logs, and does not infer completion from saved files.

Protected cards remain complete after redaction. Goal metadata and capability
cards retain reserved space; historical conversation and memory use the remainder.
An insufficient budget fails before a planner call instead of silently losing
the current instruction. Optional history is presented newest first. The same
protected material reaches the no-builder fallback. Conversation-revision fences,
budgets, approvals and existing-project snapshot handling remain in force.

Planner guidance distinguishes implementation from prose and interprets a bare
continuation in the context of saved unfinished work. Explicit research and writing
requests still select those capabilities. Research feeding implementation must be
a direct dependency of the code worker so source excerpts and URLs reach its payload.
No model, capability, worker binary, schema or execution permission is changed.

## Validation

Regression tests cover long receipt histories, complete latest instructions,
inherited revisions, explicit writer/research handoffs, inadequate budgets,
redaction expansion, no source leakage, and concurrent user steering. Existing
routing, context, writing, cancellation and manual-dispatch tests remain passing.
Static analysis and independent review found no blocking issue.

A provider-only qualification used an isolated website fixture built through the
actual ContextBuilder, with saved HTML/CSS/JS and six long assistant receipts.
The installed Ubuntu planner and unchanged generation settings were used. No user
project files, goals, jobs or tools were executed by the probes.

The initial checks selected `code.build_project` for `Continue` (27.490 seconds)
and `writing.draft` for an explicit prose request (17.049 seconds). A research
control exposed an unnecessary intermediate writer without a direct research
dependency to the code worker. After clarifying that contract, the final prompt
returned research → code in 23.114 seconds and a single code worker for `Continue`
in 17.455 seconds. All responses ended with `stop` and passed strict plan parsing.
These checks establish routing for these fixtures, not project completion.

The first four calls preserved all protected-history fingerprints. During the
final Continue probe, live state changed independently; deployment admission must
be reviewed afresh. This is not recorded as an unchanged-state verification.

Private receipts and preparation are in
`~/Library/Logs/SwarmerDeploy/website-continuation-20260928/`.

## Deployment

Not yet activated at the time this evidence section was written. The candidate
must be backported onto the exact deployed API release, with only the four planner
context services changed. The user project remains stopped with its stored files
and historical messages retained.
