# Written deliverables

A goal asking for a plan, design, report, or other text can use the `writing.draft`
worker. Its output is the requested text, not an empty deterministic synthesis
or a question asking the user to author it. Application implementation continues
to use `code.build_project`. A written plan is not evidence of implementation,
execution, tests, publication, or deployment.

The server and iPhone planners advertise only fresh, compatible, policy-allowed
agents. The catalogue reports this skill as unavailable until a text worker is
registered and heartbeating. Existing cancelled goals are not restarted by this
change. Existing conversations and their user replies remain authoritative.

## Execution and limits

`GoalManager` derives the job from the stored original objective and the newest
12 redacted conversation messages, bounded to 32,000 UTF-8 bytes. The writer uses
one reserved model call. Jobs have one attempt; a worker cannot silently retry a
model call outside the goal budget. The normal step, runtime, cancellation,
lease, policy, and concurrency checks still apply.

The new worker in `workers/text-worker` uses a configured local Ollama model. It
makes one streamed CPU request with a 120-second absolute timeout and a
512-token output cap. The prompt requests 100–140 words of plain text, five
concise steps for plans, and a short summary. Missing terminal completion,
truncation, invalid JSON,
unknown fields, invalid Unicode, empty output, or lease loss prevent acceptance.
The worker has no tools and never executes generated text. Generation failures
log fixed reason codes (for example, `token_limit` or `wall_timeout`) without
logging task data, generated text or raw exceptions.

Planner and evaluator HTTP calls default to 60 seconds. Operators running slow
CPU models can set `MONGARS_GOAL_MODEL_TIMEOUT_SECONDS` up to 120 seconds and
`MONGARS_GOAL_MODEL_CALL_LEASE_SECONDS=180`. The effective transport deadline is
clamped to at least ten seconds inside the configured lease. Provider timeouts
remain transport failures; they do not masquerade as exhaustion of the whole
goal's runtime. The goal's runtime and model-call budgets remain unchanged.

The result contains `schema_version`, `content_trust: untrusted`, `text` (at most
24,000 UTF-8 bytes), and `summary` (at most 1,200 characters). Validation enforces
the data contract; it does not certify factual accuracy. Full text remains in the
authoritative job result. Bootstrap, WebSocket goal updates, and result summaries
do not expose the raw job or duplicate the full document.

## Reading a draft

Authenticated paired devices can read
`GET /goals/{goal_id}/nodes/{node_id}/writing-draft` for a matching completed node
and completed writing job. The response includes the result fields, goal/node/job
identifiers, and SHA-256 of the exact UTF-8 text. It uses `Cache-Control: no-store`.
Unknown, mismatched, incomplete, or invalid results are not exposed.

The iPhone application API command `goals.writing-draft` and the goal screen use
this same endpoint. The client binds the draft to the current goal, node, worker
job, and connection and displays selectable plain text. Reading a draft grants
no file-write or execution authority.

## Deployment

Enroll with `python -m app.worker_admin --kind text` using the documented CLI
arguments and a private credential file. Configure `MONGARS_TEXT_MODEL_ID`
explicitly; no model is downloaded or selected implicitly. Deploy the text,
code-worker transport, and file-worker protocol siblings together. The text
worker requires `writing.draft` allowed in the effective runtime policy.

Old durable policy epochs may omit this new skill and deny it until explicit
policy reload. Schema version remains 24. Once the new policy epoch or agent is
persisted, an older server that does not recognize `writing.draft` is not a safe
rollback target; recover forward without restoring or rewriting the database.
