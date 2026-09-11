# Python code proposals

The `code.generate_python` worker produces one bounded, standard-library Python
file. It has no workspace access, write API, shell, or execution capability.
It calls the operator-configured loopback model once per leased job and validates
the returned JSON, UTF-8 size, syntax, and static imports. These checks do not
prove that the generated application works.

## User flow

1. Start a goal while the coding worker is online. The planner can select
   `code.generate_python` and pass a bounded, redacted objective.
2. When the worker returns a valid proposal, the goal waits for review. No file
   has been written. Open **Examiner le code proposé** in the goal's plan.
3. Review the source and destination. **Préparer l’autorisation d’écriture**
   binds the reviewed content digest to a fresh application task and the
   existing one-use `workspace.write_text` approval.
4. Approve or deny that task. A successful executor receipt completes the node.
   The file is saved under `generated/<goal_id>/<node_id>/app.py` inside the
   configured workspace. The program has not been executed, tested, or deployed.

The authenticated review GET is the only goal endpoint that returns generated
source. Source is not included in goal summaries, sync snapshots, notifications,
episodes, or planner training exports. The apply POST includes the reviewed
SHA-256 digest; retries reuse the same application task and tool call. An
expired or cancelled goal cannot initiate another write. Runtime and model
budgets are preserved; the generation job reserves one model call and cannot
be automatically redistributed or retried.

## Operator setup

Deploy the API, permission policy, and both `workers/code-worker` and its fixed
`workers/file-worker/file_worker.py` protocol dependency from the same release.
Install an appropriate local coding model and verify it with the worker's exact
contract before selecting it. No cloud fallback is configured.

Enroll from the Ubuntu control-plane account with its existing private database:

```sh
python -m app.worker_admin \
  --db /path/to/mongars.db \
  --permissions /path/to/permissions.yaml \
  --credential-file /private/worker-directory/registration.json \
  --model qwen2.5-coder:7b
```

Enrollment records the actual local Unix operator identity, creates an
unverified agent, and writes the one-time credential into a new mode-0600 file.
It refuses to overwrite an existing file. It adds no operator HTTP endpoint and
does not change paired-device authentication. Keep the credential outside Git
and configure the worker using its `.env.example`; do not print its value.

Run the worker as a restarting user systemd service using the release's Python
runtime. Its service environment needs only its scoped agent credential,
control-plane origin, model endpoint/model name, and bounded timing settings.
Do not give it the server environment file, database path, or workspace root.
Confirm authenticated heartbeat/claim/result flow; a healthy API alone is not
proof of a working execution agent.

Schema 21 adds `goal_code_proposals` without rewriting prior goals. Before
cutover, stop the service and back up the SQLite database, policy, environment,
and release link. Rehearse migration and integrity checks on a separate copy.
An older persisted worker-policy epoch denies the new skill until an explicit
validated policy reload installs the new rule. Rollback to schema-20 code also
requires restoring its stopped database snapshot.
