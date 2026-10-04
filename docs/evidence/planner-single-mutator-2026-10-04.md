# Counted planner grammar: single project mutator

Base: `31e230f9c4f7378b5e14cfcad63c404c2d126d57` (`main`).
Scope: generation grammar, precise rejection diagnostics and regression tests.
No deployment, goal retry, database mutation, permission change or public API change.

## Defect and correction

The counted grammar admitted multiple project-mutating steps that the independent
server validator rejected. The same public diagnostic also described the legacy
Python worker's inability to consume dependencies, even for duplicate project
workers unrelated to Python.

Capability bodies are now grouped by both dependency bounds and whether they
modify the project. For each permitted count, object alternatives allow a project
mutator in at most one slot. Other slots retain non-project capabilities. A
mutator can be first, in the middle, or last; research inputs, downstream synthesis,
independent work, hard dependencies and optional dependencies are retained.
Both `code.build_project` and `code.generate_python` share the single-mutator limit.
The legacy generator still cannot consume dependencies. No proposal is silently
merged, truncated or rewritten. The independent server validation remains active
for providers that ignore the generation schema and for evaluator proposals.

`project_plan_shape` now explains the project worker restriction without an
irrelevant Python explanation. `legacy_code_dependencies` gives the specific
legacy-input failure and instructs the planner to preserve required inputs rather
than drop them to satisfy validation. Historical stored failure text is not edited.

## Verification actually performed

The container could not resolve GitHub for cloning, so changed files were read
through the GitHub connector. The new focused test module was executed using an
isolated import harness: production grammar/decoder function bodies were executed
unchanged from their AST; only unrelated application imports were omitted. The
server shape-validator function and server-owned diagnostics were tested directly,
with small node doubles and constants read from the pinned source.

- Before the fix: **19 failed, 13 passed** in the same 32 focused cases.
- After the fix: **32 passed**.
- Python syntax compilation passed for all six changed Python files.
- The small mixed-capability schema fixture is **259,955 UTF-8 bytes**; the
  non-project fixture is **31,207 bytes**. These are fixture sizes, not a measurement
  of the full production capability catalog or of model latency.

The four exhaustive small-count cases cover all **120** ordered skill sequences
of lengths 1 through 4 over writing/project/legacy capabilities. Other cases cover
20-step plans, distant duplicate mutators, both dependency kinds, research followed
by project construction and synthesis, capability absence, definition collisions,
mutator-only capability sets, and distinct server rejection diagnostics.

The existing response-format integration tests were updated, not claimed as run.
Their former assertion that duplicate mutators passed generation is now inverted.
The full-catalog schema-size guard is raised from 100,000 to 350,000 bytes to bound
the extra slot-reference alternatives; specialist payload schemas must still occur
only once. Profiles without project-mutating capabilities keep the simple counted
shape and do not allocate unused non-project step definitions.

## Required qualification before merge or deployment

In a complete checkout with the project's development dependencies:

```sh
cd server
python -m pytest tests/test_planner_mutator_grammar.py \
  tests/test_project_plan_shape.py tests/test_planner_graph_wire.py \
  tests/test_planner_provider.py tests/test_planner_diagnostics.py
```

Then run the repository's local lint, formatting, type checks and complete server
suite. These checks were **not** run here. Validate the actual model server's
schema-to-grammar compilation, memory use and latency with the full capability
catalog; only supported object/anyOf/$ref constructs are used, but the larger
slot grammar has not been qualified against the deployed LLM runtime. Finally,
exercise the original React Native Pong request on a test deployment.

Do not infer that the screenshot's exact rejected proposal has been recovered or
that the running application is already fixed. No live planner request was sent.
No pull request was opened because this repository starts GitHub Actions on pull
requests; changes are delivered on a dedicated branch without merging to main.
