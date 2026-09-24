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

## Release scope

Only `agent_dispatcher.py`, `goal_manager.py`, `writing_contracts.py` and the text
worker runtime are release targets. No database migration, model replacement,
project restart or TestFlight rebuild is part of this correction. Production
activation and any real-model evidence are recorded separately after verification.
