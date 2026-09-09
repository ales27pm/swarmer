# monGARS research worker

This standalone worker claims only `research.query` jobs. It keeps the v0.9
agent lease alive while a query runs and submits a terminal result only while
the original lease proof remains current.

The worker has one research egress: an exact HTTPS adapter endpoint configured
by the operator in `MONGARS_RESEARCH_ADAPTER_URL`. Job payloads cannot provide a
URL, headers, credentials, or transport options. Redirects are rejected, the
adapter credential comes only from the environment, and control-plane and
adapter responses have hard byte and time limits.

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
integer from 1 through 10. The configured adapter receives exactly those two
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

Adapter content is never fetched or executed by the worker. The returned job
result is marked `content_trust: untrusted`; consumers must treat titles, URLs,
and snippets as untrusted evidence.

## Configuration and run

Copy `.env.example` into a secret-management mechanism; do not commit populated
values. Register an agent through `POST /agents/register` with the single skill
`research.query`, then set the returned one-time credential in the process
environment.

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
../../server/.venv/bin/ruff format --check .
../../server/.venv/bin/ruff check .
../../server/.venv/bin/mypy --strict research_worker.py
../../server/.venv/bin/pytest -q
../../server/.venv/bin/bandit research_worker.py
```
