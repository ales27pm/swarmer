# Offline CRM reference qualification

This is a fixed benign acceptance case for the local project worker. It covers
SQLite contacts, quotes, calendar records and email drafts. It does not send
email, call external services, create a web UI, or qualify unrelated projects.

`contract.json` defines the public API. `workflow.json` decomposes that contract
into complete modules small enough for the existing response budget. Each step
retains the full objective and previous files. The storage module receives real
tests before dependent feature modules are added; the final suite exercises the
public CRM API. A failed or truncated response is not an accepted module.

The workflow is an operator-guided reference case, not a claim that the deployed
planner automatically derives these steps. It does not replace any production
model, change runtime budgets, or resume a user project.

`acceptance.py` contains twelve independently authored checks. The generated
project's own tests and pytest configuration are excluded from this acceptance
run. The checks exercise persistence after reopening, customer isolation,
validation, exact text and the four requested feature groups. A passing generated
test, a `complete` model response, or successful bytecode compilation alone does
not qualify the project.

Run on an unprivileged Linux Docker host with a previously qualified, immutable
project-runtime image:

```sh
python3 scripts/qualify_crm_snapshot.py \
  --snapshot /private/path/result.json \
  --image sha256:THE_QUALIFIED_IMAGE_ID \
  --output /private/path/new-acceptance-receipt.json
```

`--worker-root` may select an explicitly reviewed immutable worker release when
qualifying the deployed runtime instead of the checkout. The snapshot is the
worker's cumulative JSON result containing `files`. Generated Python is executed
only in the worker's restricted Docker runtime. The tool never imports it on the
host and never contacts the production application API. A nonzero exit means
qualification failed; preserve the receipt and investigate its check output.

The runner reports the source snapshot digest, tested module digest, exact
acceptance-source hash, image ID and actual check receipts. The output path must
be new to prevent accidentally replacing prior evidence.
