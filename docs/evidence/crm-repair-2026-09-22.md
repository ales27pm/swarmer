# CRM repair and independent qualification

## Scope and current evidence

This work follows the failed benign CRM qualification recorded in
[Effective project steps](project-effective-steps-2026-09-22.md). The reference
project is an offline Python SQLite library for customers, quotes, calendar
records and email drafts. It does not send email, use external services, or
implement a UI. Generated application code executes only in restricted Docker.

The workflow is operator-guided: its module sequence and acceptance contract
are supplied explicitly. The successful run qualifies this bounded case, not
prove that the deployed planner autonomously decomposes or completes arbitrary
projects. Qualification and production deployment are recorded separately below.

## Reproduced repair failures and candidate changes

- **Wrong repair span:** pytest `path:line:` locations were not recognized like
  Python traceback locations. A test using a closed database could lead to a
  patch in the storage implementation instead of the faulty test lifecycle.
  Exact project paths now identify the enclosing function, preserving setup,
  close/reopen operations and assertions together in the bounded patch context.
- **Lost remaining work:** repair/read/no-change responses could replace the
  accepted milestone plan. Repairs retain the existing plan and latest user
  scope, including unfinished CRM features. An empty plan cannot erase the
  existing plan even when the proposed source change is effective.
- **Source starvation:** older conversation turns occupied the prompt while
  diagnostic files were removed. Exact captured inputs 12/13 from the failed
  second candidate showed no test source, then no source at all. The final
  selection now prioritizes actual traceback locations, trims older history
  before relevant source, retains the latest user instruction and bounds a
  large assistant message. Recaptured requests contain `tests/test_crm.py:3`
  as their first patch target within 21,681 and 21,294 bytes respectively.
- **Premature documentation:** passing tests for an early submodule triggered
  a README-only completion route. That route is removed; passing storage tests
  alone does not require the model to stop implementing remaining features.
- **No effective change:** a no-op completion cannot promote partial passing
  checks into completion. Disjoint operation branches and a runtime check
  reject empty/identical mutations before Docker, preserving explicit reads and
  check-only requests. The qualification driver also stops after three
  iterations without a source change; reads do not reset that counter.
- **Invalid candidate source:** changed Python files receive AST prevalidation
  before acceptance. Rejected proposals retain previous files and check
  receipts. Parsing is not execution or proof of functional correctness.

Regression coverage and the final reviewed worker digest must accompany the
final qualification record; candidate changes alone are not deployment proof.

## Independent acceptance and calibrated controls

The fixed suite in `evals/projects/crm/acceptance.py` runs **12 tests** covering
the public API, dictionary/list returns, integer IDs, validation, customer
isolation, exact Unicode/apostrophe text and persistence. Customer persistence
also requires a valid SQLite file and recovery in a fresh Python interpreter.
Generated tests and pytest configuration are excluded from this independent
run. Passing requires a successful build, twelve executed tests, zero failures
and passing check receipts.

Two **handwritten harness controls**, separate from model qualification, were
executed against the strengthened suite:

| Control | Measured result |
| --- | --- |
| Real SQLite implementation | Build passed; 12 tests passed, 0 failed. |
| Per-process mapping with a valid SQLite marker file | Build passed; 11 tests passed, 1 failed specifically on fresh-process customer persistence. |

The second control rules out the observed false-positive gap where reopening
an object in the same interpreter could appear persistent. These controls do
not count as a model-generated CRM success.

Private Ubuntu evidence:
`~/.local/state/swarmer-qualifications/crm-acceptance-controls-20260922/`.

- Acceptance source SHA-256: `190584ea0d474939938d52350a77c117689a23a4801bf23e2f57f12b9fdbc222`.
- Positive receipt SHA-256: `6b02a0e91d6eb62bc907d4080519a4aedabd422b7b4fa0d1d2141a65be3ab430`.
- Negative receipt SHA-256: `ab110c745f437b1372e36c0ea1d8978a0014aa908cfc7821abcca7a6a6ba4cf2`.
- Runtime image: `sha256:e3f3afcfd536422e962c3a58217eece5c4430ca46ed96ce06464cce8adf21082`.

## First candidate outcome

The initial standard-7B candidate run in
`~/.local/state/swarmer-qualifications/crm-repair-7b-20260922/` **failed after
eight iterations**. It produced the storage module, its test and the people
module, then stopped after three consecutive iterations without a source
change. Remaining CRM features and independent acceptance were not completed.

That run used worker source SHA-256
`c04a2f7b79772ab4a38f1839fb9a8286c6b46ce56ffdc0828d995c1d00680fb6`.
Its failure remains evidence even if a later candidate succeeds. It does not
justify promoting the 7B model or changing production budgets.

The second 7B run, `crm-repair-final-7b-20260922/`, used source
`716334ae5c43c4750984cac37bdea1f7320c050b51b5083bab8d4da59980bbe2`
and stopped after 15 iterations. It produced the four implementation modules
and two test files, but repaired the wrong file after an invalid pytest import.
Independent acceptance on that partial snapshot compiled and executed all
12 tests; **all 12 failed** because generated mixin constructors were
incompatible with the SQLite store. This exposed the source-starvation defect;
the fixture was not weakened or hand-repaired to pass.

A 30B run of that superseded candidate was deliberately interrupted after three
saved results. Its files and hashes are retained with `operator-interruption.json`
in `crm-repair-final-30b-20260922/`. It is not a final qualification result.

## Research informing the approach

These sources inform implementation choices; their benchmark results are not
measurements of this application:

- Aider documents model-dependent [edit formats](https://aider.chat/docs/more/edit-formats.html)
  and [lint/test feedback](https://aider.chat/docs/usage/lint-test.html). This
  supports evaluating bounded replacement/patch formats and returning concrete
  failures to a repair step; it does not establish which format wins here.
- The upstream [llama.cpp grammar documentation](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md#json-schemas--gbnf)
  explicitly lists mixing `properties` with `anyOf`/`oneOf` at the same schema
  level as unsupported, and warns that unsupported features can be silently
  skipped. The candidate therefore represents operation alternatives as
  distinct complete object branches instead of attaching an operation-count
  `anyOf` beside shared `properties`. The successful qualification used this
  schema through real Ollama generation; it is not an exhaustive decoder test.
- [EvalPlus, arXiv:2305.01210](https://arxiv.org/abs/2305.01210) shows that stronger
  test suites uncover incorrect generated programs missed by weaker suites.
  The local application is an independently authored acceptance suite calibrated
  with positive and negative controls, not adoption of its benchmark scores.
- [RLEF, arXiv:2410.02089](https://arxiv.org/abs/2410.02089) studies training models
  to use execution feedback across repair steps. It motivates measuring whether
  repairs improve actual results. No RLEF training or fine-tuning was performed
  in this work.

## Final qualification and deployment

### Model-generated result

The production model `swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e`
completed the guided CRM from an empty snapshot in **seven iterations** with
**346.09 seconds** of cumulative iteration time. Every iteration added the
expected file without changing previous files. The final generated suite ran
four passing tests; the separate fixed acceptance suite compiled the modules
and ran **12 passing tests, zero failures**, including fresh-process SQLite
persistence. No implementation files were manually repaired between iterations.

Evidence directory:
`~/.local/state/swarmer-qualifications/crm-context-30b-20260922/` on Ubuntu.

- Qualified worker commit: `c4390bd083aa14eacbc4369864b6b8c73edd593f`.
- Qualified worker SHA-256: `81d8ba8d571b6fdd4d2efd317743e9659a06376fc0727e1018a173194532cf79`.
- Final snapshot SHA-256: `ee06f61844db772c6a47194cfbf8903c1904bdf6b9d5b9017bef945e406f18b7`.
- `summary.json` SHA-256: `3537ec1baf65287c83ed8d8576d5d2ef751b845c52e568736d01f0c61968c10a`.
- `acceptance-7.json` SHA-256: `871f30a67f29e835caf1269ef571f473433003623037a50f7d6a522db7bb0670`.
- Independent read-only attestation SHA-256: `92a248a2e27f5c2c416ae3414d94df925fa4ff06e50ccb721006d87b21cb909b`.

All seven calls had `compact_repair=0`; the largest prompt was 21,564 bytes.
A subsequent narrowly scoped compact-creation correction prevents duplicating
the latest user request for Python tests, Node tests or a Node manifest after
a model timeout. Three 11,682-byte CJK regression cases fail before this change
and pass after it. This compact branch was not exercised by the successful
model run.

The final production backport is `d83ffc0503e2514ecd30416497914eeb30ce6272`,
worker SHA-256 `a2706e819a15cd06b3c25e58fc30f56851f1dc02f256dd6290eef5816fdc5dbc`.
An intercepted-transport replay of all seven saved inputs proves that its
outgoing HTTP request bodies are byte-identical to the qualified worker's,
using the exact production transport dependency. All seven other release
sources remain identical. This replay made no model calls. The compact change
is covered by the separate regression cases, not claimed as a second live run.
Private equivalence evidence:
`~/.local/state/swarmer-qualifications/crm-context-request-equivalence-20260922/equivalence-evidence.json`
on the iMac, SHA-256
`b166e90ef85089523be26c9cc84293f6ee4f57a7c5b050bf47d876a691574cae`.

Final main-tree worker validation: **317 passed, 5 skipped**, Ruff and strict
mypy passed. The isolated production backport passed **313 tests, 5 skipped**,
Ruff and strict mypy; the four-test difference belongs to context fields already
in main but excluded from this production backport. API diagnostics validation:
**136 passed**, with two existing deprecation warnings.

### Production rollout

Both guarded cutovers completed successfully on **2026-09-22 Montreal time**.
They used idle admission checks, a SQLite writer barrier, immutable source
inventories, coherent backups and supervised recovery. The API remained on
schema 24; canonical initialization on a private database copy preserved all
57 tables. Neither rollout restored a database or resumed cancelled work.

An initial worker preflight stopped before any service change because its
mount check incorrectly expected the host launcher inside the sandbox. The
reviewed launcher mounts seven runtime sources and then replaces itself with
`bwrap`. The corrected deployment checks retain all eight hashes on disk and
verify the seven mounted files separately. Deployment-helper regression suites
passed **69 tests** (27 worker, 42 API), including absent required mounts and a
changed host launcher.

| Component | Active immutable release | Independent verification |
| --- | --- | --- |
| Project worker | `d83ffc0503e2514ecd30416497914eeb30ce6272-b0f3175752c2` | `2026-09-23T00:41:32Z` |
| Control-plane API | `7f840874e47cb049b3d35982083570a895938b5c-0197f0049fcf` | `2026-09-23T00:42:46Z` |

The API wheel SHA-256 is
`0197f0049fcfb5b3d45b7de6a8707f7060c5aa07528479cbbdcf30cc917d4331`.
Its only application-source change is `services/project_progress.py`
(`0a3578497c99af2178667b38b9db4cfcb8039f3e8ac8824fbebb840debb7d092`).
The final verification checked the running API, project sandbox sources,
unchanged worker bindings, credentials, model settings and fresh authenticated
heartbeats from all five workers. All **35 protected history fingerprints**
remained identical, including **273 project revisions**. API health was `ok`.
The coupled API and worker services restarted successfully; other worker source
releases were preserved.

Private Ubuntu release evidence:

- `crm-context-worker-final-deploy-20260922/`: reviewed baseline
  `45631af9efb140d6d5d4893b3b9706ec4d64a62a06069c5e81ff25b6fdbf0dc5`;
  cutover receipt `0285fc801fe799271174a6c76e6bf7bd6fbf06c8a177213f04a9d9a8b07a7438`;
  independent receipt `a0f712128a76e5d9c3616f9bf2c22405cf9b356a6fb89586bf9469d014a17814`.
- `crm-repair-api-20260922/`: reviewed baseline
  `05208a0387080433909b656159a3d32967877549fd9f00159020ed7b47628165`;
  cutover receipt `89938bed791e7f4a3beb7464102e8f85636f6ed76223bb42ea434a518e58e20d`;
  independent receipt `ad56df4e42fed3d8f1e80d7ffaee7543a3e676952384daffaa1314b99ea9a72a`.

These are backend changes. No iPhone runtime test or new TestFlight binary is
claimed by this release record. The qualified CRM remains a guided offline
library case, not a guarantee about arbitrary projects or autonomous planning.
