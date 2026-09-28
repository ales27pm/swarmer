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
Static analysis and independent review found no blocking issue. On main, 153
selected server tests passed (82 routing/context/provider, 32 runtime/writing,
28 ContextBuilder and 11 continuation regressions); the independent decline
contract/runtime checks also passed.

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
Read-only audit inspection traced this to an iPhone continuation accepted at
02:39:02Z. Production planning started after that probe ended and repeated the
old writer route. The attempt failed with the declared outcome; saved project
revisions were unchanged. No history was rewritten to obtain idle admission.

The isolated release candidate `aefc7434d835fe3441f06fbf1015f378c99068d3`
backports main `a96f468` onto deployed API `ebd11524`. Its four-file runtime delta
matches the intended patch. The existing Swift-only capability prompt was retained
instead of importing unrelated specialist work from main. The exact candidate was
therefore independently qualified with the same fixture:

| Latest request | Returned plan | Duration | Output tokens |
| --- | --- | --- | --- |
| Continue | One `code.build_project` | 20.752 s | 424 |
| Explain only; do not edit | One `writing.draft` | 15.513 s | 362 |
| Research then finish site | `research.query` → `code.build_project` | 24.328 s | 590 |

All three candidate responses passed strict parsing and preserved all 37
protected-history fingerprints. Candidate tests: 110 passed. Deployment-helper
tests: 108 passed. Archive/wheel inventory, RECORD and unchanged dependency
metadata were independently verified.

Private receipts and preparation are in
`~/Library/Logs/SwarmerDeploy/website-continuation-20260928/`.

## Deployment

Candidate and exact rollback staging succeeded with no production service change.
Both private-copy migration checks retained schema 26 and preserved all tables.
Wheel SHA-256: `279a98a256f84d05742137e6175cdf5053614662c52523a1646b3eb9b4316100`.
Source archive SHA-256: `8dd4f0301803c1d7606ffdf1c7c501388b5d107b0f257a2082177b0fbadadfb0`.

The first preparation attempt stopped at the idle-admission guard when another
user goal appeared. No service was interrupted and no new passive-state exception
was introduced. That goal ended naturally at 02:47:35Z with a declared decline.
A fresh read-only check found all eight admission counts at zero at 02:47:42Z.
Its legitimate activity was reviewed before capturing a new baseline; no goal was
cancelled and no stored history was edited to make deployment admissible.

The guarded cutover then completed successfully. The API process runs from
`aefc7434d835fe3441f06fbf1015f378c99068d3-279a98a256f8` and reports healthy.
All six restarted Ubuntu services are active, and six enrolled agents have fresh
authenticated heartbeats. Planner and evaluator model identities/settings,
worker source/runtime files, bindings and credentials remain unchanged. All 37
protected-history fingerprints match the reviewed pre-cutover baseline. No
database restore or agent re-enrolment occurred; saved projects and revisions
were preserved. The memory-status route check verified authentication enforcement
only; it did not validate authenticated memory contents.

A separate read-only verifier passed at 02:51:58Z on the exact activated release:
API health, fresh authenticated heartbeats, unchanged model/configuration/worker
identities, all 37 protected fingerprints, and all eight activity counts at zero.
It made no model calls or service/goal changes. Private verification receipt
SHA-256: `30fdad03b9716d7515dc946343373929c338e655ed49a62ff31f10eebe35bcba`.

No user project was automatically resumed. The provider tests establish routing
on the described benign fixtures; they do not establish completion of the user's
website or physical-iPhone runtime validation. This is an API-only deployment and
does not require a new TestFlight binary.
