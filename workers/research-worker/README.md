# monGARS research worker

This standalone worker supports `research.query` and `research.collect` jobs.
It keeps the v0.9 agent lease alive while a job runs and submits a terminal result only while
the original lease proof remains current.

The operator selects one research provider. The default, `adapter`, uses an
exact HTTPS endpoint configured in `MONGARS_RESEARCH_ADAPTER_URL`. The optional
`searxng` provider uses a separately operated local SearXNG instance. Job
`research.query` payloads cannot provide a URL, headers, credentials, or transport options.
`research.collect` can additionally read explicitly supplied public source URLs
under the separate bounded page policy described below.
Redirects are rejected, credentials come only from the environment, and
control-plane and research responses have hard byte and time limits.

The built-in adapter transport canonicalizes its credential-free HTTPS URL,
does not consult proxy environment variables, resolves the hostname exactly
once, and rejects the complete DNS answer set if any IPv4 or IPv6 address is
not globally routable. It connects directly to a validated numeric answer,
checks the peer before and after TLS, and verifies the certificate for the
configured hostname. One absolute monotonic deadline covers DNS, connect, TLS,
request, headers, and response reads. Both compressed wire bytes and expanded
JSON are capped at 256 KiB; unsupported or malformed compression fails closed.

## Job contract

Skill: `research.query`

```json
{
  "query": "A question of at most 2000 characters",
  "max_results": 5
}
```

The payload rejects additional fields. `max_results` is optional and must be an
integer from 1 through 10. The default HTTPS adapter receives exactly those two
fields and must return exactly:

```json
{
  "results": [
    {
      "title": "Source title",
      "url": "https://source.example/item",
      "snippet": "Short source extract"
    }
  ]
}
```

For `research.query`, result URLs are never fetched or executed by the worker. The returned job
result is marked `content_trust: untrusted`; consumers must treat titles, URLs,
and snippets as untrusted evidence.

## Configuration and run

Copy `.env.example` into a secret-management mechanism; do not commit populated
values. Register an agent through `POST /agents/register` with the single skill
`research.query`, then set the returned one-time credential in the process
environment.

`MONGARS_RESEARCH_PROVIDER` defaults to `adapter`; unknown values fail at startup.
To use local search, set:

```sh
MONGARS_RESEARCH_PROVIDER=searxng
MONGARS_SEARXNG_URL=http://127.0.0.1:8080/search
```

The SearXNG URL must use HTTP, the numeric host `127.0.0.1` or `[::1]`, and
exactly `/search`, with an optional port. Hostnames, other addresses, URL
credentials, queries, fragments, redirects, and proxies are rejected or unused.
The worker connects directly to the numeric loopback address and checks the
connected peer. Adapter tokens are not read or sent in this mode.

Enable JSON in the SearXNG instance's `search.formats` configuration. The worker
uses the documented [SearXNG search API](https://docs.searxng.org/dev/search_api.html):
a form POST containing only `q` and `format=json`. It maps `title`, `url`, and
`content` into the existing result contract, filters malformed results and
duplicate URLs, truncates titles to 300 and snippets to 4000 characters, and
returns at most `max_results`. An empty results list is valid. SearXNG's upstream
search engines and their network access are configured separately by the operator.

Both providers use `MONGARS_RESEARCH_TIMEOUT_SECONDS` (1–60 seconds, default 20).
One absolute monotonic deadline covers the complete local HTTP operation,
including slow headers and body chunks. Compressed and expanded responses are
each limited to 256 KiB. SearXNG form requests are capped at 32 KiB to accommodate
percent encoding of the existing 2000-character query limit.

```sh
python3 research_worker.py --once
```

The control plane must expose the existing authenticated endpoints:

- `POST /agents/{agent_id}/heartbeat`
- `POST /agents/{agent_id}/claim`
- `POST /agents/{agent_id}/jobs/{job_id}/heartbeat`
- `POST /agents/{agent_id}/jobs/{job_id}/result`

It must also add `research.query` to its dispatch validation and permission
allowlist with the payload contract above. This skill performs network-backed
research, so it must not be added to the server's automatic lease-retry set.

## Local verification

From this directory, using the repository server virtual environment:

```sh
../../server/.venv/bin/ruff format --config ../../server/pyproject.toml --check .
../../server/.venv/bin/ruff check --config ../../server/pyproject.toml .
../../server/.venv/bin/mypy --strict research_worker.py
../../server/.venv/bin/pytest -q
../../server/.venv/bin/bandit research_worker.py
```

## Evidence collection (`research.collect`)

The separately advertised `research.collect` capability accepts a focus and **one
to four explicit complementary search queries**. The planner chooses queries
from the user's requested dimensions; this worker does not invent a topic,
expand a goal, call another model or retry automatically. Older workers continue
to accept only the unchanged `research.query` contract.

```json
{
  "focus": "Compare the requested persistence, search and update properties using official documentation",
  "queries": [
    "site:docs.example.org engine persistence transactions",
    "site:docs.example.org document format serialization updates"
  ],
  "max_results_per_query": 3,
  "max_pages": 4,
  "source_urls": ["https://docs.example.org/known-page"],
  "required_domains": ["docs.example.org"]
}
```

`max_results_per_query` is 1–5 (default 3); `max_pages` is 1–6 (default 4). Search
results are interleaved by query rank before selecting pages, so the first query
cannot consume every page slot. Up to six explicit canonical HTTPS `source_urls`
can also be supplied from established source context. These are attempted first,
within the same total page limit, even if the search engine fails. No document
path is guessed. Sources supplied directly have an empty search snippet and no
query indices unless they also appeared in actual search results. Search results retain their original `title`, `url`, `snippet`
and zero-based `query_indices`; their text is **not** claimed to be a page read.

The version 1.1 receipt contains `searches`, `results` and separate `pages`. Each
search has a timestamp, result URLs and a completed/failed status. Each page has
the requested URL, attempt timestamp and read/failed status. A successful read
also has its final URL, HTTP 200, supported MIME type, extracted text, truncation
flag, SHA256 of the response body and SHA256 of the exact extracted UTF-8 text.
Failures have a bounded reason code, never response bodies or credentials.
`source_urls` preserves the admitted direct URLs. `coverage` reports required
domains without a successfully read page and queries without page evidence,
separately from collection status. Domain matching uses exact host or subdomain
boundaries, never substring matching. This is source presence, not semantic
coverage. The server recomputes it from the exact page receipts; older version
1.0 receipts remain readable without invented coverage.

`collection_status=complete` means the admitted searches and selected reads were
performed, **not** that every user requirement is covered or that the text is
correct. `partial` retains useful search snippets and any successful page reads;
`no_evidence` records no returned sources. There is no invented fallback content.
Downstream validation must still check the requested deliverable. The server
also validates that queries, per-query limits and round-robin page selection
match the admitted job payload.

Page reads use GET and public HTTPS/443 only. Every redirect (at most 3) is checked
again. The reader resolves and validates the complete DNS answer set, pins the
numeric connection, verifies its peer and TLS hostname, and sends no cookie,
authentication, proxy-derived header or browser state. Private/mixed DNS answers,
nonstandard ports and HTTP downgrades are refused. It never executes scripts or
follows links in downloaded documents.

The whole collection has a 120 second deadline; each search is capped at 20 seconds
and each page (including redirects) at 15 seconds. Cancellation or lease loss is
checked during DNS/I/O waits, between chunks and during HTML extraction; an
in-flight connection, including TLS handshake, is closed and cannot be adopted
later. Unavailable leases discard the result through the existing worker policy.
DNS resolution retains the existing single bounded resolver thread slot.

Bodies are limited to 1 MiB, with a 32 KiB accepted-header bound; content encoding must
be identity. Only HTML/XHTML/plain text with an allowed charset is extracted.
PDFs, authentication walls, unsupported encodings, invalid Unicode and oversized
responses become explicit failed page receipts. HTML extraction drops script,
style, head, navigation, footer and explicitly hidden elements; it does not
attempt browser rendering or infer CSS from external stylesheets. Large documents
produce at most 4000 characters of actual passages ranked by lexical focus and
associated search terms, with adjacent qualifications where the budget permits, not a
generated summary and not necessarily in source order. These bounds and lexical
ranking do not establish semantic completeness.

The server projects up to 4000 characters per page excerpt with a second
`excerpt_sha256`; the full extraction's `content_sha256` remains distinct. A
redirected page cites its final verified URL. The writing/context budget can omit
whole sources; the original bounded receipt remains available for inspection.
New payload fields require a coordinated server/research-worker rollout; do not
send them to a previously deployed collect worker. No service is registered or
deployed by adding these source files.

Validation includes both legacy tests and the local, network-free collection
corpus:

```sh
../../server/.venv/bin/pytest -q .
../../server/.venv/bin/mypy --strict research_worker.py research_collect.py
../../server/.venv/bin/bandit research_worker.py research_collect.py
```
