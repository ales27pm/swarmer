# Personal worker: CRM and documents

Implements `crm.command` and `documents.extract` using the existing file-worker's authenticated claim, background lease heartbeat and generation-fenced result protocol. No model, shell access, outbound-mail tool, calendar registration or worker enrollment is performed by this directory.

## Deployment

Deploy this directory alongside `workers/file-worker/file_worker.py` on Ubuntu, in a dedicated OS account with access only to its approved workspace. Configure the standard `MONGARS_SERVER_URL`, `MONGARS_AGENT_ID`, `MONGARS_AGENT_CREDENTIAL`, and `MONGARS_WORKER_ROOT`. Never register a skill the installed profile cannot run. The API's permission approval and node arguments remain authoritative; a job is not permission to change credentials or target host.

For CRM, configure `MONGARS_CRM_ORIGIN` as a bare HTTPS origin, `MONGARS_CRM_ALLOWED_ORIGINS` as a comma-separated exact allowlist, and `MONGARS_CRM_TOKEN` as a server secret. Credentials never belong in a task, iPhone app, result or document parser process. Redirects and environment proxies are disabled. The fixed destination path is `/api/integrations/v1/commands`. The counterpart service and additive migration live in the CRM27PM repository (`docs/service-integration.md`); an installed local worker alone does not enable the remote endpoint.

Run `python personal_worker.py --once` for one claim or omit `--once` to poll. Only one claim is processed at a time in-process. A result whose lease has expired is discarded; the CRM may already have committed, so recovery must retain the original key.

## CRM payload and receipt

```json
{"operation":"tasks.create","data":{"dealId":"existing-deal-id","title":"Préparer la soumission"},"idempotency_key":"approved-task-stable-key"}
```

Resources: organizations, contacts, deals, tasks, documents. Actions: search, read, create, update. Optional keys: id, query, limit, data, idempotency_key. **Create/update requires a key assigned to the approved operation by the control plane, stable across every job retry.** This worker never generates a replacement. The key becomes an HTTP header and is omitted from the command body. Nothing can select an endpoint or supply a token in job input.

Results contain the service's persisted receipt; they are untrusted content, not new user instructions. HTTP400/401/403/404/409/413 return bounded error codes. Network/5xx after mutation produces `crm_outcome_unknown`: never assume rollback or create a new key. Retry the exact approved command/key or read the remote entity to recover. Service idempotency gives one effect despite a lost worker result. No local fake CRM success is produced when configuration is missing.

## Document payload and provenance

```json
{"path":"imports/brief.pdf","max_characters":20000,"max_pages":100}
```

Only relative PDF/DOCX/UTF-8 TXT/Markdown paths beneath the configured workspace are supported. File-worker descriptor-relative traversal rejects symlinks, dotfiles, secret-like names, absolute paths and `..`. Source bytes are capped at20MB, pages at200 and text at100000characters; result bytes at524288. A SHA-256 of the actual opened source bytes and original relative path accompany every success. TXT/Markdown has no invented page numbers. Docling exports Markdown and up to100 source-item references carrying real page numbers where available; missing page provenance remains empty, and truncation/partial/empty extraction is explicit.

PDF/DOCX conversion runs in a separate parser process with a120second deadline; lease loss kills it. The child receives source bytes through stdin and no CRM or worker credentials. It has offline HuggingFace/Transformers settings, disabled remote enrichment/plugins, and the converter accepts only local byte streams. Deploy with OS network denial for defense in depth. Plain UTF-8 extraction has no optional dependency.

Optional profile: create a **separate** Python3.12 virtual environment and install `requirements-docling.txt` (`docling==2.130.0`, checked against upstream release source). Pre-provision the required model artifacts and set `MONGARS_DOCLING_ARTIFACTS_PATH`. No model download is initiated here. PDF OCR is deliberately disabled in this initial profile and produces a warning; scanned PDFs may produce a partial/empty result. This is not a claim of OCR qualification. DOCX uses its document backend without a PDF model-artifact requirement.

Docling is not installed or import-qualified in the present development environment. Missing imports/artifacts report `docling_unavailable`/`docling_artifacts_unavailable`; they never become completed extraction. Real PDF/DOCX corpus qualification and installation of models remain a release gate for advertising those formats. The passing local tests exercise real UTF-8 files, symlink rejection, missing-dependency isolation, an HTTP test server for the exact CRM transport contract, and lease fencing.
