# Explicit writing refusals are not completed drafts

## Defect and change

The writing lane previously validated JSON shape, size and citations without a
separate model-declared outcome. A refusal could therefore look like a completed
text deliverable. This correction does not change models or attempt to make a
model answer a declined request.

New model responses must explicitly declare `outcome: delivered|declined`.
Delivered responses retain the exact existing public draft shape, including for
current TestFlight clients. Declined responses use a separate bounded contract
with text, summary and model identity taken from the worker configuration, not
from generated metadata. The worker submits a failed job after the usual final
lease/cancellation check. There is no additional inference or automatic retry.

The server validates the declined result, retains its untrusted text and records
`model_declined`. The goal manager reloads authoritative job/task/node evidence
and checks the conversation revision under a transaction. A current refusal
fails the node and goal atomically, with an explicit French conversation message
identifying the model and explaining that its statement is not a verified server
conclusion. Existing terminal-goal handling cancels remaining unfinished work in
that goal. A newer pending user instruction prevents an old refusal from ending
the updated goal. Saved project files and accepted history are retained.

This is an explicit outcome protocol, not a semantic refusal classifier. Old
results are not rewritten. A model can still mislabel a response; a delivered
JSON object alone is not proof of substantive task satisfaction. Quoted refusal
words in an ordinary draft do not trigger heuristic rejection.

## Verification

A regression first reproduced the missing diagnostic: the old worker turned a
structured refusal into generic validation failure, losing the declared outcome
and model identity. Separate server regressions reproduced inappropriate success
or continued evaluation before the fix.

The final integrated run passed 285 tests spanning the worker, declined contracts,
writing, citations, research handoff, runtime recovery, continuation planning and
agent dispatcher. Ruff passed from each package's normal working directory;
mypy passed on the three changed server modules. Existing Starlette/httpx
compatibility deprecation warnings remain. An independent reviewer found no
blocking issue in compatibility, persistence, cancellation or conversation races.

Regression coverage includes delivered wire compatibility, missing/invalid
outcomes, bounded model identifiers, forged model metadata, citation validation,
lease loss, cancellation, duplicate events, forged event mappings, durable source
relationships and a newer user reply arriving before result handling.

The complete text-worker directory subsequently passed 173 tests, after updating
two inherited citation-transport fixture expectations to the private model schema.
The narrow API backport passed 178 tests. These runs overlap the integrated suite;
their counts must not be added together.

## Actual model request

On September 24 at 05:11 UTC, one bounded request used the installed writer
`swarmer-planner-qwen2.5-coder-abliterated:7b-32k-a416f57` to draft a French family
agenda. It explicitly returned `delivered` in 50.99 seconds. The worker removed
the private outcome field and the server validator accepted the unchanged public
four-field success contract. No production job or database row was created.
The private SSH forward was closed, and the tested source hash remained unchanged.

This verifies an actual successful model response under the new contract, not
exhaustive task quality or the behavior of every model. Declined-result handling
is covered by deterministic regression tests, not by soliciting a harmful output.
The private receipt SHA-256 is
`f8260abdb9068423acb2ddbeca280521ef14fd14c437af666e46d2877d23e0ac`.

## Release scope

Only `agent_dispatcher.py`, `goal_manager.py`, `writing_contracts.py` and the text
worker runtime are release targets. No database migration, model replacement,
project restart or TestFlight rebuild is part of this correction. Production
activation used isolated backports over the deployed sources, not the full main
branch. At 05:21 UTC the API changed to
`0a4c9e0151dc1282ce19fec2761d0035b22fc1e3-e8976a26d44d`; at 05:23 UTC the text
worker changed to `37fa1a062829a71064e41e8752740c8364d94781-94359ef397c3`.

Both supervised cutovers and their independent verifiers passed. All 37 protected
history fingerprints were unchanged, including 14 projects and 321 revisions.
The API health check returned `ok`, all six agent identities had fresh heartbeats,
and no work was active at admission or post-verification. Model aliases,
credentials, policies, schema 26 and worker identities were unchanged by this
source-only release. A separate root SSH check confirmed the API release and all
three changed source hashes. The existing TestFlight build needs no reinstall for
this server correction.

The first inactive staging attempt rejected an invalid wheel transport basename;
its directory was retained and staging retried with the correct filename and
identical bytes. This did not activate or stop a service. No rollback was needed.

Independent receipt SHA-256 values:

- API: `15794bdcd2510676e747fb3e4088baad43b3e1792c8a0f1708cb79a975c50547`
- Text worker: `dec9b844f41d91d15d9660b8842479ef563b9aea71de2d3b17f8b2022008b389`
