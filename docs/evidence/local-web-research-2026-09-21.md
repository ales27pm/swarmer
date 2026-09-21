# Local web research qualification, 2026-09-21

Qualified source: `0690e0e8adfb290079b495240158ea619ebdff5c`.
Initial implementation: `4ef112b198c4203058a43322ec789b05904e898b`.

The requested product is a personal assistant with connected tools. Coding remains
one capability. The planner now distinguishes web research, sourced writing and
software implementation. A `research.query` node precedes a dependent
`writing.draft` node when the requested answer needs current sources.

## Source and regression evidence

- 555 combined server, research-worker and text-worker tests passed on
  `4bd6c7a` (39.44 seconds), following the initial 427-test integration run.
- 40 mobile planner tests passed, including preserving research-to-writing
  dependencies and rejecting a researcher which has disconnected.
- Mobile TypeScript/ESLint, affected Python Ruff checks, strict mypy and worker
  Bandit checks passed.
- Research tests use real loopback HTTP servers for form encoding, absent bearer
  credentials, no proxy/DNS/redirect, response limits, compression and deadlines.
  The existing global HTTPS adapter remains the default and retains its checks.
- Required research dependencies are checked against their goal, node, task and
  completed worker job. The writer receives up to five redacted source excerpts
  in a separate `research_sources` field, with an 8,000-byte source limit and a
  32,000-byte total payload limit. Historical payloads without the field remain
  unchanged. Source URLs are retained exactly or omitted, never truncated into
  different links. Neither the writer nor the research worker opens result URLs.
- A private structural qualification completed using the real GoalManager and
  dispatcher with explicitly simulated planner, search, writer and evaluator.
  This establishes integration structure, not real model judgment or web access.

## Existing SearXNG and isolated gateway

The existing SearXNG instance in `/home/ales27pm/original-monGARS` was reused.
Its container and its outbound proxy were not restarted or reconfigured.
A separate gateway publishes only `127.0.0.1:8721`, accepts only `POST /search`,
removes sensitive request headers and disables request logging. Its upstream is
the fixed Docker alias `searxng:8080`. The configuration and operating procedure
are in [local web research](../34-web-research.md).

An actual public search for library services in Sorel-Tracy returned 20 results
and 19,663 response bytes, including the municipal library page. It used zero
model calls. `GET /search` and `POST /config` both returned 404. Container
inspection verified its non-root user, read-only root, empty capabilities,
no-new-privileges and resource limits. The host bind is loopback only.

The initial internal-only network did not publish the port under the installed
Docker version. The new gateway therefore also uses its own ingress bridge.
This gives Caddy a possible outbound route; its fixed proxy configuration is an
application restriction, not an operating-system outbound firewall. The original
SearXNG outbound proxy and its network restrictions are unchanged.

The body bound was subsequently aligned to exactly 32,768 bytes and the running
configuration revalidated. That accommodates a form-encoded 2,000-character
Unicode query. Earlier failed gateway checks stopped only the newly created
gateway and left the historical stack intact.

## Real-model qualification

The first private real-model run used source `4ef112b` and stopped after
58.077 seconds. The planner selected the correct research-to-writing graph,
but copied the entire user instruction into the search query. SearXNG returned
three unrelated sources. The writer correctly reported that those sources did
not describe Sorel-Tracy library services. The qualification rejected this
answer before calling the evaluator: it did not count transported URLs or a
completed generation as a successful researched answer.

That failed run used one planner call, one search and one writer call; no retry
occurred and no production goal state changed. Its retained receipt SHA-256 is
`4411a74d7c7d42c93bb6564a3f8c5d8cf2cef90580393721f1677c6cdeefdebf`.

The first correction explicitly told both planners that a research-node objective
is sent verbatim to the search engine and must contain only the relevant query.
Writing instructions stay in the dependent writer node. The second real run
revealed example anchoring: despite a complete library objective in its context,
the planner copied the example's swimming-pool subject. The writer then mixed
library and pool claims, added an unsupported contact URL, and the evaluator
incorrectly accepted the answer. This run is a semantic failure regardless of
its pipeline status. Its retained receipt SHA-256 is
`db7961dbf62d4b32de284555ca2ba75b29fcd6a9428a382d779884e58fc47b9c`.

A separate qualification-script defect kept renewing an already completed job
while evaluation ran; this raised a terminal-job conflict after the evaluator
returned. It does not explain or excuse the semantic failure. The harness now
stops job renewal after result acceptance and retains the agent heartbeat.

Commit `9dae612` removes concrete subject examples from both planners; commit
`270552c` strengthens the evaluator's subject, location and source-support checks.
The 39 server planner, 40 mobile planner and 63 evaluator tests passed. The
transport, schema and runtime acceptance criteria were not weakened. Both failed
runs are retained, and neither was deployed.

Replaying the preserved incorrect answer against the strengthened evaluator still
returned `done`. A second replay after changing schema property insertion order
also failed. Captured transport evidence showed that this Ollama path sorted the
response fields alphabetically: `completion_summary` was emitted first despite
the requested order. The latter request used 3,044 input tokens with a verified
32,768-token runtime context; the observed error was not attributed to truncation.
These failed replays each made one evaluator call and no production mutation.

Commit `4bd6c7a` introduces numbered, private model-transport field names so
lexicographic grammar ordering places invalid evidence and missing requirements
before the verdict and completion summary. Strict decoding restores the unchanged
public decision fields and rejects duplicates, mixed names and unknown fields.
Legacy public-name responses remain parseable for compatible endpoints. This
source correction passed 97 focused evaluator tests before real-model replay.
The subsequent replay confirmed that numbered fields were emitted in the intended
order, but the current 30B code evaluator still accepted the wrong-topic answer.
The transport change therefore does not establish semantic reliability. The
positive chain and production activation remained on hold while installed
general-purpose models were assessed separately, without changing production
role settings.

The installed Hermes 3B abliterated model copied a concrete CRM example from the
evaluator instructions. Commit `8e4dbd6` removes that example and the example
Python-language detector; 96 focused evaluator tests passed. New negative replays
still failed semantically: the 30B code evaluator accepted pool evidence for a
library request, while Hermes returned a contradictory terminal verdict and new
coding node, which the existing authoritative validator rejected. Neither model
was qualified for research evaluation by these tests.

A separate model-selection probe used the already installed `qwen3.5:9b` with
`reasoning_effort: "none"`. Its digest was
`6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`.
It correctly identified pools versus libraries and returned a research-to-writing
replan in 16.821 seconds (2,849 input and 412 output tokens). The request, response
and explicit semantic review were retained; receipt SHA-256:
`a9449b12d8cd9790b50407e6b1dd07fa96d7a1c2e63b7770920347319b0021aa`.
This standard Qwen model is not described as abliterated. This probe changed its
private request configuration, not production bindings, and does not by itself
qualify the full assistant workflow.

Commit `783b9a0` adds an optional `MONGARS_RESEARCH_EVALUATOR_MODEL` setting.
It is used only for a canonical graph containing research, writing and synthesis,
with completed nonempty research still present in the bounded evaluator context.
Mixed code/workspace and legacy nodes preserve the normal evaluator even when
the context budget omitted those nodes. The chosen provider is recorded in the
durable model-call reservation before inference; one evaluation consumes one
call. The setting is off by default. The optional provider sends
`reasoning_effort: "none"`; the default provider's transport remains unchanged.
146 focused and neighboring tests passed, including 25 new routing/configuration
tests. An independent 115-test subset and source review also passed, along with
Ruff, strict mypy and Bandit. These are source checks, not production activation.

The private full workflow on `783b9a0` then stopped safely at the writer after
94.884 seconds. The unchanged 7B code planner still produced an instruction-like
search query, yielding mostly general municipal, BAnQ and social results.
The unchanged 7B writer produced an unsupported citation, rejected before
canonical result acceptance. There was one planner call, one real search and one
writer call, no evaluator call or accepted draft. Receipt SHA-256:
`82fa8b07fd727d95e20b53df4cc7017a5bd4316da69083aa6c605ad15d274d97`.
No automatic retry or production change followed. The native optional-evaluator
route and single durable reservation had passed a simulated lifecycle, which
does not replace the missing positive real-model result.

Commit `e6a5eac` adds optional native planner reasoning control without changing
model defaults. 95 focused tests passed, including an independently run 89-test
subset. A private comparison then used Qwen3.5 9B for planning, writing and
research evaluation; the normal 30B evaluator remained configured separately.
The writer retained its native `think: false`, CPU-only execution, 512-token
limit and 120-second deadline. This run failed after 91.865 seconds: its query
was still an instruction sentence, and the resulting draft omitted source URLs
and misinterpreted a 2015 archive as current library information. The draft
passed the worker's schema validation but was stopped by the qualification's
citation requirement before canonical submission. This is not evidence that
production rejected those factual errors. No evaluator call occurred.
Receipt SHA-256:
`9272c358beca151bed478efbce758561031d6a3cd41f9b2278721fb717b62e36`.
The alternative model profile was not activated.

Commit `5d7ffff` separates the model's search input from generic task instructions:
research nodes use the private wire field `search_query`, which is translated to
the unchanged public `objective` after bounded, duplicate-safe JSON parsing.
The dedicated grammar branch is available only when `research.query` is advertised.
Other node types retain `objective`; mixed fields and misplaced aliases are rejected.
Both planner proposals and evaluator recovery nodes use this mapping. Legacy
objective-only responses remain compatible, and the public API itself continues
to reject private aliases. 37 focused wire tests and 204 neighboring tests passed;
an independent 159-test subset, source review, Ruff, strict mypy and Bandit passed.
The tests cover exactly-at-limit legacy JSON, oversized inputs, non-finite numeric
exponents, duplicates, unavailable skills and unchanged public serialization.

Planner-only model probes on `5d7ffff` did not qualify that candidate. The original
7B model proposed three independent drafts without research (12.361 seconds),
and the general 9B model assigned a research-titled node to `code.build_project`
(13.292 seconds). Neither probe ran any search or worker. Their receipts were
retained with SHA-256 `f285f1f1801f5e98e6d72660047434704856cff4c4298f6d173c53ceae8d623b`
and `261cefa3db9d7ae9ba221f3298cfa6c48d01a12122510d19e221eb8de12b01eb`.
The raw response order exposed a new branch-selection concern: generating
`objective` before `required_skill` excludes the research branch, which requires
`search_query`. This motivated an explicit first-position capability discriminator;
the observed output alone does not prove a working correction.

Commit `b4bd149` makes `00_required_skill` the first sorted wire key of every
proposed node, translated back before query decoding. The public `required_skill`
name and legacy responses stay unchanged; mixed or misplaced names are rejected.
171 targeted tests, Ruff, strict mypy and independent source review passed.
The subsequent original-7B probe actually emitted the discriminator first and
produced `services des bibliothèques de Sorel-Tracy sources officielles`, with
a research-to-writing dependency. This proves the observed wire order and query
correction for that probe. It also added a synthesis without any input; the full
plan was not qualified. A helper assertion tried sorting a null synthesis skill
with strings and raised TypeError; raw and parsed responses were preserved.
No worker was executed. Receipt SHA-256:
`3f3a3ed355337618af31b4c5c11cb1f8ec5cdfc7f655dfb5e3a8f77796282d96`.

The corresponding 9B probe still assigned the research task to the project builder.
Unlike the 7B endpoint, it emitted properties in insertion order, with the skill
discriminator last. Receipt SHA-256:
`1e55b1f7c6ad7dd998c1dfefc1f5ae855e5c491029f32e4c6168504a696164cd`.
Commits `c3b2c9a` and `a90eb22` place the discriminator first in both property and
required-field insertion order. When a writer is advertised, synthesis must have
at least one required input; real aggregation remains available. An evaluator test
fixture initially inherited a dependency despite describing itself as inputless;
that fixture was corrected before the final 173-test passing run.

The subsequent 7B probe on `a90eb22` produced a concise library-services query but
replaced the writer with a dependent synthesis (10.107 seconds, 2,167 input and
364 output tokens). Canonical graph validation passed, but the qualification
correctly rejected the absent writer. No search or downstream model call ran.
Receipt SHA-256:
`0e212f479c88fd289c6fe1d105e703b6db017a46f9b361ab88ed0a1faed1a101`.
Review found contradictory pronouns in the planner instructions attributing actual
text generation to synthesis. Commit `8256726` explicitly documents the runtime:
synthesis only concatenates bounded existing summaries, while `writing.draft`
invokes a model to write, summarize, translate or analyze. The evaluator received
the same distinction. 173 focused tests passed after the correction.

The planner-only probe on `8256726` then included both real workers with the
required research-to-writing dependency (11.740 seconds). It also retained a
dependent deterministic synthesis; that is redundant, not a replacement for the
writer. Receipt SHA-256:
`bc3e77346618d82c18978c49edb5858da2cfefa77c5b92f43a095183457fd4bc`.
The complete private run stopped after 94.847 seconds at the production citation
guard: the writer shortened a supplied Facebook post URL into an unsupplied page
URL. No draft was accepted and no evaluator call occurred. The dependent
synthesis completed from actual research input without an empty/skipped step.
Receipt SHA-256:
`bb7107f95ffafa103deef9ab94bd1e3f7aa29ce7626630064df41cfb3d88b5a3`.

A direct two-query comparison through the same live gateway isolated search
quality from model generation. The verbose query ending in `sources officielles`
returned generic municipal pages and old archives. The direct query
`bibliothèque Sorel-Tracy services` returned the official municipal library page
first, followed by the MRC library directory and local library-services pages.
Both returned 20 results, with no model calls. At that observation, Google CSE
supplied results while Brave, DuckDuckGo and Startpage reported suspension or
CAPTCHA; no rate-limit or CAPTCHA bypass was attempted.
A controlled follow-up removed only `sources officielles` from the original
query, leaving `Services des bibliothèques de Sorel-Tracy`. It likewise returned
the municipal library page first and MRC directory second, with excerpts about
the online catalogue, reservations and information/cultural resources. Thus the
quality-label difference was checked independently of the other wording changes.
Commit `503a2fd` keeps source-quality requirements in expected output and writer
criteria instead of adding those labels literally to query terms. It makes the
same distinction in both server providers and the mobile planner; 134 server
tests passed. This prompt change needs its own real-model qualification.

Commit `ccd2b29` adds a deterministic citation-provenance check both in the text
worker and in the server's result-acceptance transaction against its persisted
payload. The exact bad real draft is rejected for its invented `/contact` URL.
Tests verify that rejection leaves the job and canonical plan node incomplete,
does not expose draft text in errors, preserves unsourced writing, and accepts
exact supplied links in normal prose/Markdown. This check establishes URL
provenance, not the semantic truth of a claim. Ambiguous punctuation on a URL
query or fragment is rejected rather than changing its destination.

Commit `728e8f6` removes long-URL copying from sourced generation. The private
model request presents source IDs, hostnames, titles and excerpts. It retains
the original objective and conversation verbatim; URL tokens inside untrusted
source titles/excerpts are omitted. The model selects bounded unique IDs and may
use matching markers. Unknown/duplicate IDs, unselected markers and generated
HTTP(S) URLs are rejected. The worker inserts exact original URLs into the final
text before reapplying the unchanged canonical byte limits and citation guard.
Unsourced requests, the public response contract, leases and generation budgets
remain unchanged. Empty selected IDs allow an honest insufficient-evidence draft;
they do not prove that a requested sourced answer was delivered.

An independent review caught prefix-based replacement of a user-authored URL;
the final implementation preserves all objective/conversation text instead.
224 worker/server writing tests passed, as did an independently run 82-test
citation subset, Ruff and strict mypy. The retained Facebook regression verifies
that the previously shortened source cannot be accepted through the new wire
format. This establishes exact link provenance, not factual correctness.

The real `728e8f6` run verified the citation change: the writer selected S1/S2,
the worker inserted both original URLs (including the long Facebook post URL),
and the canonical server accepted the 1,197-byte draft. All three graph nodes
completed, with real inputs for synthesis. The overall qualification still
failed: the 7B planner retained the poor quality-label query, and the resulting
answer did not establish the requested library services. The 9B evaluator
recognized insufficient relevant evidence, but returned `failed` with a proposed
new synthesis, rejected by the authoritative terminal-decision contract. It also
incorrectly claimed the French draft was not French. There was no retry.
Receipt SHA-256:
`f88b683d1a5e7558a499fa988c6810ae6ddb478649f560c8028f65a5bbabd10b`.

A separate 9B planning probe produced a clean research-to-writing graph in
11.599 seconds but used `services bibliothèque municipale Sorel-Tracy officiel`.
A direct search with that exact query likewise returned weak or unrelated
sources. Thus this comparison did not qualify a model replacement. Production
retained the original 7B planner/writer settings during this probe. Receipt SHA-256:
`a937671b58ac35f9e73c331d47a1328140d969adb8d315b8ad2834aad313d0e8`.

Commit `528a020` fixes the terminal-decision grammar without relaxing the public
validator. The old node alternatives had a sibling `maxItems: 0` for terminal
statuses; the native grammar still allowed a recovery node. Terminal statuses
now use a standalone empty-array rule. Six regressions reproduce an `anyOf`
converter that drops sibling constraints; 138 focused tests, Ruff, mypy and
independent review passed. The upstream
[grammar converter](https://github.com/ggml-org/llama.cpp/blob/master/common/json-schema-to-grammar.cpp)
also treats unions and array repetition separately. Installed-runtime behavior
is verified independently below; static schema validation alone is insufficient.

The next positive scenario uses an explicit search query. It is an additional
tool-use qualification, not a replacement for the failed natural-language request
for official library-service sources. Query formulation remains a documented
model-quality limitation; exact citations do not resolve it.

The real insufficient-evidence replay on `528a020` returned a valid `failed`
decision with no proposed nodes in 11.145 seconds. It did not falsely mark the
unsupported answer done. The incorrect French-language criticism remained, as
did an overbroad criticism of source officialness; this was recorded rather than
treated as correct reasoning. Receipt SHA-256:
`6f93145b4008d9dce36afb68119c352c1f99877456808123fb9668ed24711b19`.

The additional explicit-query scenario then failed during original-7B planning
after 18.887 seconds: six identical synthesis IDs depended on a nonexistent
research node. Canonical validation rejected the duplicate ID before search,
writing or evaluation. No retry occurred. Receipt SHA-256:
`3ebaeef669fe599fa4d06f7f234587664dba6876cdf9da417b12c5d24859f75f`.
That is a planner-quality failure, not evidence that SearXNG failed. A final
controlled comparison changed only the planner to the installed 9B model while
preserving the exact explicit-query objective, writer, evaluator and helper.
It selected the correct query and research-to-writing graph in 17.667 seconds,
but copied the goal UUID into temporary IDs of 67 and 74 characters. The public
64-character bound rejected the plan before any search. Receipt SHA-256:
`ae2b84183598ea0a2eaefb8103a883c89704ac543b94b5c0c517aac98ff0a7b4`.

Commit `0690e0e` encodes the existing public identifier length into the private
model grammar. For the known identifier pattern, the public maximum derives the
bounded suffix repetition; other patterns and long text fields are unchanged.
The instructions require short proposal-local IDs rather than goal UUIDs.
187 focused tests, Ruff and strict mypy passed. The tests cover planner and
evaluator grammars, dependency IDs, 1/64-character acceptance, 65/67/74-character
rejection, character rules and preservation of the public schema.

## Successful explicit-query workflow

The full native-model run on `0690e0e` completed in **85.286 seconds**, using:

- Planner: installed `qwen3.5:9b`, native `reasoning_effort: none`.
- Writer: existing `swarmer-planner-qwen2.5-coder-abliterated:7b-32k-a416f57`.
- Research-only evaluator: `qwen3.5:9b`, native `reasoning_effort: none`.
- Normal/code evaluator: separately configured existing 30B model, unused here.

The unchanged objective was: “Recherche sur Internet « bibliothèque Sorel-Tracy
services » et rédige un bref aperçu en français avec les liens trouvés.
N’effectue aucune réservation.” The model produced the exact concise query and
two valid dependent worker nodes, with 30/36-character temporary IDs. There was
no manual replacement of query, plan or identifiers.

One real SearXNG worker search returned five relevant sources. One writer call
produced an accepted 985-byte French overview, with all five original URLs
resolved from source IDs. Independent review checked each claim against the
returned snippets: online catalogue, named municipal branches, MRC information,
separate college resources, the city map and library programming. The draft
distinguished the Cégep library from the municipal libraries. One evaluator
reservation completed with `done`; both nodes completed and none were skipped.
Exactly three model calls were charged. No retry, reservation, approval or
production goal mutation occurred.

The retained receipts are under
`~/Library/Logs/SwarmerDeploy/web-research-20260921/real-explicit-query-0690e0e-qwen35/`:

- `receipt.json`: `531f49be4ae09f565eaec02ec575847c0bc49e039fa7b528aafdab382af0848e`.
- `semantic-review.json`: `95be8e01ca202b64ddb6b6e15e0dda4c3e4795c5f85d44659e21748ceee2b192`.
- `accepted-draft.txt`: `d8096e5df8888a08fdbcaa29be14e902741adfcb7d156120b7d6f2b6fc622217`
  (export includes a trailing newline; canonical text hash is
  `a23a8d7c4c5d1c01ec0f15d52f4aa91a0f1d9777b911ba10fb678023f9589b7e`).

This proves the explicit-query basic-overview scenario only. The original
natural-language request for official sources remains unqualified. The earlier
negative evaluator's incorrect French-language criticism is still a known
weakness. Evidence consists of search snippets, not full-page verification;
long source URLs can also displace snippets in bounded evaluator summaries.
The research operation is recorded as an authenticated worker job, so an empty
generic `tool_calls` table does not mean that no HTTP search was executed.

The user's pending local-plan goal is preserved. It has no dispatched node or
active server model call. Any deployment admission exception must match its
reviewed complete goal/task fingerprints, audit revision and project revision;
new activity invalidates the exception before interruption.

## Server activation

The API and text-worker update was activated under bounded systemd supervision
on 2026-09-21 at approximately 08:18 UTC. The deployed source is `0690e0e`, release
`0690e0e8adfb290079b495240158ea619ebdff5c-b7649ede467e`. All 66 installed API files
matched the archive, dependency checks passed, and canonical initialization on
a private database copy left schema 24 and all 57 tables unchanged.
The deployment-helper suites passed 57 tests. Independent review found no blocker
in the identifier grammar or exact three-field model-profile change.

The production profile was verified in the running processes: planner 9B with
native reasoning disabled, optional research evaluator 9B, original writer 7B
and normal/code evaluator 30B. The final environment preserves every other byte
and parsed setting. No model was downloaded. The 120-second model deadline and
180-second model-call lease remain unchanged.

The admission transaction confirmed zero active server-work counters and the
exact approved passive goal exception before stopping services. The API became
healthy and all four existing agents returned online with unchanged identities
and credentials. The project worker's qualified c4 source and read-only worker
sandbox checks passed. The pending goal, root task and project revision 65
retained their exact fingerprints. A consistent backup was retained; no database
restore or existing-agent reenrollment occurred.

Local deployment receipts are in `~/Library/Logs/SwarmerDeploy/web-research-0690e0e/`:

- Source archive: `4fdc68312b87114857734e3eb485d856068bf30e554916501f1a2c9876950f1f`.
- Wheel: `b7649ede467eb0dd1494833d9ba5cdfea1d23617228e52a5fbf530433439eea9`.
- Reviewed baseline: `6560616e83e957e57101c2b4e5399e32cab7481561a0b6f4ebed0cb39efa6852`.
- Cutover helper: `737cd1288cfcae218dde68bc264df78499a86573496d62a7402f5161eed18651`.
- `cutover.json`: `aff957a1cb6b2226039193cd5d11cc82ad00e2cce8baf6b6130657ca7ec2855b`.

## Installed research service qualification

The new `ubuntu-web-researcher` was enrolled through the canonical agent registry,
with only `research.query`, one concurrent job and a private saved credential.
Its service uses the expected research source, a read-only mount, no effective
capabilities and `no-new-privileges`. It connects to the local SearXNG gateway.
Enrollment did not restart any existing service or change the schema.
The activation receipt SHA-256 is
`458b13d252a2e5455a7d072797b6a534a8e0c35f87900f8ec62cae5fc54f3730`.

A separate, explicitly operator-authored qualification task used the canonical
task insertion, audit and job-dispatch mechanisms. It did not impersonate an
iPhone or user-device request. The installed service polled, claimed and
completed `job_21c1fd660ecc4921a611627c5cb00f43` on attempt 1, lease generation 1,
for task `tsk_20dfcfdf8d8147dbbca6348dff9c621a`. The creation, queue, claim and
completion audit IDs were 1875, 1876, 1878 and 1881. No manual result injection
or retry occurred. The job returned the municipal library page, MRC directory
and Cégep library-services page for `bibliothèque Sorel-Tracy services`.

The same enrolled agent, worker PID and source hash were verified before and
after execution. The result SHA-256 is
`d28fa92bb056693c2761dbe0ec8f3b78a0d8ac8763ccd9ed8bc31308b92f82c3`.
The user's pending goal remained unchanged. This proves the installed worker's
authenticated poll/lease/result path, separately from the private full-model
qualification and separately from any physical-iPhone test.
The local receipt is `research-qualification-1789978846605874811.json` in the
deployment directory. Its `model_calls: 0` value is a source-derived declaration,
not a database measurement. An independent consistent read-only database snapshot
at 08:23:43 UTC confirmed the actual completed job, agent-authored claim/result
audit events and matching result digest. No goal-model-call reservation was
created during the task's 08:20:42–08:20:46 UTC execution window; the task has no
associated goal or plan nodes. All eight admission counters were zero and the
passive goal's exact fingerprint still matched. This review made no model or
search requests. Evidence: `research-qualification-independent-db-proof.json`.

## iPhone artifact

The earlier Debug iPhone candidate `20260921060000` built successfully from `270552c`.
The audit verified its Development signature, device inclusion in the profile,
172 unchanged mobile/native sources, dependencies and the final research-query
and subject-preservation text inside the Hermes bundle. IPA SHA-256:
`6380a2973f7ad5f543c8ae330b412fcaf36e60b3d6779eb9e3eda3b879f0b237`.
It is not installed. The post-build check found the device paired, booted, with
the development tunnel connected and DDI available. However, all existing app
API sessions had expired, so a passive check could not establish that no local
generation or user interaction was active. No expired token was reused and no
app launch, screen interaction or physical API qualification was claimed.

The final Debug candidate `20260921073357` was rebuilt from `790565f` with both
the deterministic-synthesis and source-quality distinctions in its local planner.
61 mobile tests, TypeScript and ESLint passed. The 07:36:54 UTC build audit verified
the Development signature, dependencies, 172 unchanged mobile/native sources and
the final prompt marker inside the bundle. Its IPA SHA-256 is
`75c0c95aa0e6c795f74e13788adb17a7a954ba8c78dd82035814a0825aa53e65`.
Subsequent commits through `0690e0e` change only backend code and tests;
they do not alter that mobile tree. This IPA remains uninstalled, with the earlier
inactivity question pending. No device API execution or TestFlight upload is claimed.
