# monGARS research worker

This standalone worker claims only `research.query` jobs. It keeps the v0.9
agent lease alive while a query runs and submits a terminal result only while
the original lease proof remains current.

The operator selects one research provider. The default, `adapter`, uses an
exact HTTPS endpoint configured in `MONGARS_RESEARCH_ADAPTER_URL`. The optional
`searxng` provider uses a separately operated local SearXNG instance. Job
payloads cannot provide a URL, headers, credentials, or transport options.
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

Result URLs are never fetched or executed by the worker. The returned job
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
