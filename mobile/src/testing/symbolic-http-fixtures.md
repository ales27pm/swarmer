# Symbolic HTTP fixtures

Captured on 2026-10-03 from a disposable local FastAPI/SQLite application using
the public paired routes: `/memory`, `/memory/concepts`, `/memory/proposals` and
`/memory/search`. Setup uses the helpers in
`server/tests/test_symbolic_retrieval_acceptance.py`. Embedding, normalization
and presentation providers were absent, and socket connections were prohibited.
No production memory, pairing credential or authorization header is included.

- `symbolic-http-ordinary.json`: a source-qualified proposal found by a concept
  label absent from the source text, with the complete `only_after` condition.
- `symbolic-http-astral.json`: a valid identity containing 600 supplementary
  Unicode characters, counted as characters by the Python contract.
- `symbolic-http-large-integer.json`: an applicability integer of
  `9007199254740993`. JavaScript JSON parsing cannot represent that integer
  exactly, so the mobile reader must reject the whole evidence card. Exact large
  quantities remain representable as typed `lexical_value` strings.

Opaque IDs, timestamps and hashes are captured fixture data. A matching shape
does not establish truth, current source visibility or authority. The mobile
parser does not reconstruct Python's claim fingerprint from JavaScript numbers
(for example `1.0` and `1`, or negative zero).

The application API still refuses reserved property names and redacts credential
keys. When those policies would change symbolic applicability, the result fails
explicitly; it is not returned as a partially redacted claim. Extending the
transport to support every Python JSON value losslessly remains separate work.
