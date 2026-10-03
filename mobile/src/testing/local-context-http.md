# Local-context public HTTP fixture

`local-context-http.json` contains four unmodified JSON responses from the real
FastAPI `/memory/local-context` route with paired TestClient requests and a
disposable SQLite database: goal planning, tool proposal, whole budget omission,
and explicitly disabled retrieval. No production data, credentials, model calls
or network connections were used. The `now` field is the fixture extraction
time; tests use it to check the issued 30-minute expiry without moving dates or
replacing the server's actual receipt identifiers and digests.

The producer uses `symbolic_seed`, `prepare`, `goal_setup` and `no_network` from
`server/tests/test_memory_local_context.py`. It calls `prepare` normally, then
`goal_setup`, then `prepare(max_context_bytes=0)`, then sets the test app's
symbolic catalogs to an empty tuple and calls `prepare` again. The complete
result is serialized with `json.dumps(..., ensure_ascii=False, indent=2)`.

The source and extraction receipt are private at
`/tmp/swarmer-local-context-public-ssuv0c8f/`; the checked-in responses remain
usable after that temporary directory is removed. Parser tests verify the
Python-to-TypeScript wire boundary, including all claim conditions and source
bindings. They do not establish the semantic quality of a live model, current
production authorization, or proof of execution on the iPhone.
