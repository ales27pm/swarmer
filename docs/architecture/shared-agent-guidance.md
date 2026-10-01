# Shared guidance, requirements and observed experience

The server assembles a versioned context capsule for model-driven project work.
It is independent of the model's prose and semantic memory rankings. It does not
change tool permissions, job leases, execution budgets or acceptance criteria.

## Sources and priority

1. Runtime permissions remain enforced by the server and workers.
2. User requirements retain their original source IDs in chronological order.
   Repeated text retains every source and the priority of its latest occurrence:
   "SQLite, then JSON, then SQLite" must still end with SQLite after deduplication.
   New user instructions may revise earlier instructions; a summarizer cannot.
3. `server/app/data/AGENTS.md` is the reviewed, packaged operating guide.
4. Accepted project `AGENTS.md` files apply from the project root to the matching
   directory. A nested guide's scope ends at that directory's subtree.
5. Historical observations and generated proposals are advisory data.

Project guidance comes from accepted project revisions. Writing a new guide in
an attempt does not change that attempt's permissions or instructions. Original
source hashes and revision IDs are retained even when displayed text is redacted.
The common guide's displayed bytes must match its hash exactly.

## Handoffs

The common `durable_context` wire contract contains the state version,
fingerprint, complete user requirements, base revision, common operating guide,
scoped project guides and historical observations. The same strict validator is
mirrored in standalone model workers; launchers pin and bind those files.

Planner, evaluator, project builder, writer and legacy Python generator receive
the applicable capsule for project work. Deterministic filesystem, research and
Git tools retain their narrow validated argument contracts; a guide never gives
them a new capability. Their execution receipts are available to downstream
model roles through the existing dependency and evidence paths.

For eligible writing repairs, `retry_of_node_id` links a failed attempt to its
next proposal. The server checks the goal, revision, job, original payload and
measured diagnostics before sending `previous_attempt_feedback`. There is no
implicit latest-failure lookup or additional uncharged attempt. Changed
historical observations do not invalidate a repair, while changed mandatory
requirements or applicable guides do.

## Learning boundaries

Observations are reconstructed from linked project nodes, jobs and accepted
revisions, not from an agent announcing success. The current projection examines
the latest 32 terminal jobs and includes up to 6 usable observations with an
explicit omitted count. Complete receipts remain in the database.

Measured writer failures keep their structured discrepancies. Other failures
without a validated diagnostic remain reported failures, with no invented cause
or remedy. Accepted code snapshots can reference recorded checks, but an older
check cannot establish correctness of changed files. A historical containing
revision is not automatically the revision on which a check executed.

This is a basis for learning from experience, not a guarantee against repeated
mistakes or an automatic training pipeline. A future reusable-procedure layer
must validate scope, preconditions, source evidence and changed-code applicability
before promotion. Cross-project promotion is not implicit.

## Bounds and failures

The capsule has a 32 KB serialized limit; the common guide has a 4 KB limit.
Workers additionally account for the full model request. Mandatory requirements
and guides are never silently truncated. An oversized request fails explicitly.
Compaction may remove only source-backed, covered assistant discussion.

Required dependency overflow also fails explicitly. Optional context can remain
bounded. Source revisions and conversation checks protect against racing user
updates. These guards are separate from model quality: retained instructions
still need independent output validation and real execution tests.
