# Planner dependency generation — 2026-09-27

## Observed failure

Read-only inspection of the reported goal confirmed successive planner rejections for
unknown dependencies, a cycle, and a self-dependency. A later call naturally produced
an accepted five-node plan before this fix. Raw rejected proposals were not retained,
so their exact text cannot be reconstructed. The operator did not resume, cancel,
modify, or execute this project.

The reproduced general defect is a contract gap: the previous generation schema
accepted arbitrary dependency identifiers that the strict public DAG validator correctly
rejected. Corrective hints alone did not prevent the repeated failures.

## Change

Only the initial planner's model transport changes. Its `nodes` value is a bounded
chain of at most 20 steps with fixed consecutive IDs. Each step can reference only
earlier steps through required or optional dependencies. The next link represents list
order and never adds an execution dependency; independent work remains parallelizable.

The decoder preserves every declared edge and node body, rejects malformed wrappers
or references without silently repairing them, and then invokes the existing public
validator. Existing public array proposals remain compatible. Capability allowlists,
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

## Narrow deployment candidate

The deployment candidate is based on the installed `5cc1e3d5c86a735a65be0f78641e073ed0d163cb`
baseline. Only `planner_provider.py` and the new `planner_graph_wire.py` differ in the
runtime. The provider change is the isolated graph-transport patch from `ce6b358`,
not a copy of newer, undeployed specialist integrations. Existing Swift argument
schemas and all other baseline runtime modules remain unchanged.

The eight affected test files pass together: **268 passed**. Evaluator, plan-validation,
and goal-manager integration checks have **87 passed**, with two existing evaluator
`maxLength` expectation failures reproduced separately on the untouched baseline and
excluded from the bounded integration rerun. Ten stale exclusive-project-shape test
failures were also reproduced on the untouched baseline; their fixtures and assertions
were updated to reflect its already deployed mixed-DAG validator, which is unchanged.
The new shared-body size check uses the baseline Swift specialization instead of a
newer SQLite specialization. Ruff and strict type checks pass for both changed runtime
modules; affected test lint passes. No production deployment or real-model success is
claimed by this candidate preparation evidence.
