# Offline CRM qualification

This fixed reference case asks the real local model to build an SQLite CRM with
customers, quotes, calendar records and email drafts. `contract.json` defines the
public API. `workflow.json` divides the work into seven small modules/test/docs
steps. This is an **operator-guided workflow**, not an autonomous planner score.

Run on the Linux Docker/Ollama host, using the exact worker source intended for
deployment and the existing immutable project runtime image:

```sh
python3 scripts/run_crm_qualification.py \
  --worker-root /path/to/reviewed/worker-release \
  --work /path/to/new/private/qualification \
  --model LOCAL_MODEL_ALIAS \
  --image sha256:IMMUTABLE_RUNTIME_IMAGE_ID \
  --max-iterations 16 \
  --admission-db /path/to/mongars.db
```

The destination must be new. Each iteration records its input, cumulative result,
model/transport metrics and actual isolated check receipts. A new production job,
model call or embedding request interrupts qualification. Three consecutive
iterations without a source change stop the run, including repeated reads. The
driver does not resume user projects, write to the production database, change
model settings or retry inside a worker iteration.

When all planned files exist and their generated checks pass, the driver runs
`acceptance.py` independently. Only root `crm.py` / `crm_*.py` implementation
modules enter this container alongside the fixed tests; generated tests and
pytest configuration are excluded. Generated source executes only in the
restricted Docker runtime, never on the host.

A pass requires compilation and all **12 acceptance tests** to pass. Coverage
includes integer IDs, dictionary results, validation, exact Unicode/apostrophe
text, customer/quote/event/draft persistence and reopening SQLite from a new
Python process. A valid SQLite header alone is insufficient. Emails remain
drafts and no network access is available.

To recheck an immutable saved result separately:

```sh
python3 scripts/qualify_crm_snapshot.py \
  --worker-root /path/to/reviewed/worker-release \
  --snapshot /path/to/qualification/result-N.json \
  --image sha256:IMMUTABLE_RUNTIME_IMAGE_ID \
  --output /path/to/new/acceptance-receipt.json
```

Compare `summary.json`, the last result, acceptance receipts and source hashes.
Model prose, a completed action or passing model-written tests alone are not a
pass. Admission or handled runtime interruptions retain the last saved result
and a non-passing summary.
The handwritten positive/negative harness controls are calibration evidence;
they are never counted as model-generated application results.
