# Planner dependency generation — 2026-09-27

## Observed failure

Read-only inspection of the reported goal confirmed successive planner rejections for
unknown dependencies, a cycle, and a self-dependency. A later call naturally produced
an accepted five-node plan before this fix. Raw rejected proposals were not retained,
so their exact text cannot be reconstructed. No project operation was performed during this initial inspection. Later, on the
user’s explicit instruction to stop the active evaluation, the canonical cancellation
API cancelled the goal/run and fenced its current worker job. All four stored project
revision rows had the same content fingerprint before and after cancellation.

The reproduced general defect is a contract gap: the previous generation schema
accepted arbitrary dependency identifiers that the strict public DAG validator correctly
rejected. Corrective hints alone did not prevent the repeated failures.

## Change

Only the initial planner's model transport changes. Its `nodes` value declares
`00_node_count` before `01_steps`. The chosen count (one to twenty) selects exactly
that many required step slots with fixed consecutive IDs. Each step can reference
only earlier steps through required or optional dependencies. Slot order never adds
an execution dependency; independent work remains parallelizable.

The decoder preserves every declared edge and node body, rejects malformed wrappers,
count mismatches or references without silently repairing them, and then invokes the
existing public validator. Previous chain and public array proposals remain compatible. Capability allowlists,
specialist arguments, synthesis input requirements, legacy generator limitations,
single project-mutator rules, approval boundaries, budgets, and cooldowns are unchanged.
The evaluator keeps its existing transport, including references to existing results.

Body definitions are shared rather than duplicating specialist schemas at every step.
The grammar uses `$ref`, `anyOf`, `const`, and `enum`, supported by llama.cpp b10091,
the version pinned by Ollama 0.32.3. Generic JSON Schema validators can revisit invalid
deep alternatives expensively; production uses the bounded decoder and public validator,
not generic JSON Schema validation on model output.

## Verification

The regression initially accepted unknown and self dependencies in the old grammar
and rejected them in the public parser. The implementation was also tested against
pass-through stubs before adding the constrained encoding and decoder.

Tests cover unknown/future/self references, maximum depth, malformed wrappers, graph
field shadowing, edge preservation, independent work, legacy responses, capability
and argument restrictions, query aliases, charged calls, stale-call fencing, cooldowns,
and recovery. Independent comparison found the evaluator schema byte-for-byte equal
for 193 capability combinations. Static analysis and the focused integration suites
pass. Runtime probe and deployment evidence are recorded below when verified.


## Live model qualification

The first native probes using only prose instructions returned a valid one-step chain
while their rationale claimed multiple deliverables. Both finished with `stop`, well
below the token limit. The linked grammar itself worked when three steps were required
in a diagnostic fixture. A complete nested example was therefore added to the actual
prompt, explicitly distinguishing list order from execution dependencies and requiring
all requested deliverables before the final null link.

With the ordinary one-to-twenty-step grammar and that prompt, the installed Ollama
planner returned three valid steps in 18.696 seconds: research, writing dependent on
that research, and an independent directory listing. This was one model call and no
goal, worker, tool, or database write. It verifies this benign multi-agent fixture;
it is not evidence that arbitrary generated projects are complete.

Private probe receipt: `planner-dependency-20260927/probe/candidate-20260927T231553Z/receipt.json`.
Response SHA-256: `d38e58eb0fbdaf704586f342558a35ba2a3ad837f75e356cf3a71bd7e3379ac2`.
The prompt example is also decoded and validated against the production contract in
a regression test, which confirms that independent example steps remain independent.

A single-deliverable control with the same prompt returned exactly one writing step,
no dependencies, and parallelism one in 17.592 seconds. It used one model call and
created no tasks or database writes. Receipt: `probe/candidate-20260927T232323Z/receipt.json`.
Response SHA-256: `d7c09b275e96dfd42fbf129680d571f2ebcaebd68307b02c608462563dd484c9`.

## Guarded deployment and post-deployment observation

The isolated two-file runtime candidate `5b0807c9d8d9123a79fdcca05cab736c720af57f`
was deployed to Ubuntu using the existing immutable release lane. Its wheel SHA-256
is `36cf0c9f9305b44b2c3ad9e1886c2d068cbb67157fa9a074d9d9c97131c2db93`.
Independent verification at 2026-09-27T23:33:04Z confirmed healthy API service, six
worker heartbeats, unchanged runtime bindings and all 37 protected data fingerprints.
The configured model and policy were retained. No pending evaluation was resumed.

A separate post-deployment benign three-step probe returned HTTP 200 but hit the
2048-token completion limit after 70.172 seconds. Its text repeated the directory
listing through step 19 after the three requested operations. The incomplete JSON
was rejected; no goal, worker task, tool execution or project write was created.
Response SHA-256: `139f005a3c6bb15ca225e70e1b61751bbd3af6451d15af438d5d599988f4c3f4`.
This contradicted any claim that two successful earlier probes establish reliable
termination. The final counted structure enforces the number of steps chosen before
the model emits their bodies; it does not guarantee the quality of the chosen plan.

## Counted structure and project graph release

The actual 9B model was tested with the final counted schema and unchanged context,
token budget and model settings. The three-deliverable fixture returned exactly
research → writing plus an independent listing in 30.450 seconds (701 output tokens,
`stop`). The single-deliverable fixture returned one writing step in 9.592 seconds
(302 output tokens, `stop`). Neither call created a goal or executed a tool.

Receipts: `project-graph-20260928/probe/candidate-20260927T234820Z/receipt.json`
and `candidate-20260927T234903Z/receipt.json`. Response SHA-256 values:
`9cb5c70369a8a45be75455567a865ea3956f63fc9c90c9f40f80123bc662f9a3`
and `253c7ed68b1943f5682ae53c93ea3175d3cbaeff992293164c7e3dccc91960b6`.

Candidate `ebd11524a545aa9da77bfe03915453af7d7b60e9` adds the read-only graph API
and accepted-plan public explanations alongside the counted planner. Its six-file
runtime delta was independently checked against the deployed predecessor. The wheel
SHA-256 is `9bfb606e29610822fe6722231729e7df32dfca5b42aeb6659bc103ed20986c33`.
The isolated candidate passed 296 tests; two runtime model-call-count assertions
failed identically on the untouched predecessor and remain a known test limitation.
All 110 deployment-helper tests passed, including changed-history admission failures.

The supervised deployment completed successfully. Independent verification at
2026-09-28T00:02:46Z confirmed the candidate API, six online workers, unchanged
model/worker/policy bindings, zero active jobs and all 37 protected data fingerprints
unchanged. Database schema remains version 26. No user goal was resumed.
The byte-identical predecessor is retained for rollback without restoring the database.

A post-deployment provider-only call completed in 26.462 seconds with the same three
steps and required dependency, 701 output tokens and `stop`. The normal schema was
used without forcing a minimum step count. All 37 protected fingerprints matched
before and after the call; no task or project write occurred. Response SHA-256:
`3fc4ac41cf5545d2024d5cfc14a034fb514c6ef954e06093b392de83da8ea08e`.

The installed graph reader also read the cancelled reported goal: ten nodes, seven
edges, one recorded evaluation and the latest linked revision. It correctly returned
no planning explanation for this older plan. The live HTTP route rejected an
unauthenticated request with 401; authenticated HTTP was covered by automated tests,
not exercised against production in this read-only smoke check. Data fingerprints
again remained unchanged.
