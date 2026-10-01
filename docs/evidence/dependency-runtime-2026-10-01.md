# Dependency runtime qualification — 2026-10-01

## Scope

The project worker runs generated applications in a Docker image distinct from
the API environment and Ubuntu host. The failing TODO application imported
Selenium, but the active project image had neither its Python package nor a
browser or driver. Installing Selenium in the API virtualenv would not repair
that runtime.

This change introduces reviewed dependency recipes, actual-image inventory,
static Python import diagnostics, and preservation of those diagnostics in the
next model request. Unknown import names are not mapped to guessed registry
packages. Existing project version pins remain authoritative. A correction uses
the normal recorded project iteration and isolated installation stage.

## Source and local checks

- Three operator recipes lock 51 distinct Python distributions with SHA256
  hashes. The optional Python web recipe is not an API upgrade.
- The mobile manifest now pins TypeScript to the version already resolved in
  its lockfile, 5.9.3. No Expo or React Native version was changed.
- Final worker validation: 342 tests passed; five opt-in Docker tests were skipped in
  that local command. Real Docker evidence is recorded separately below.
- The dependency catalogue audit has 22 passing tests, including conflicting
  recipe versions and concrete mobile lock versions outside the manifest.
- The final harness SHA256 is
  `0c01a10a6b15c9adc186bea3f8fa668bcbd9b27e0921b1bd071d84246924f743`.
- The actual outbound model-request test preserves all 13 missing imports under
  context pressure, after raw logs are reduced to 200 characters. The compact
  diagnostic includes provenance and is bounded to 4,000 bytes.
- Expo's locally installed CLI reported dependencies up to date under Node
  24.19.0 with `CI=1 expo install --check`. This is not a native iPhone build.

## Initial candidate: runtime evidence

Candidate image:
`sha256:f0256d1abd3ed8682d6bee2d52254dd6d6c5ff37bde7d9decc5f5b523da79607`.

The candidate extends the existing immutable runtime. Its Debian package
resolution uses the signed 2026-09-30 snapshot. Core and browser Python recipes
install with hashes and wheels only; `pip check` passed. Browser binaries and
their inventory were produced during image construction.

Two real Python checks passed inside this image:

1. A missing-library fixture reported `missing_dependencies` with zero tests
   executed, instead of pretending test collection succeeded.
2. A standard-library fixture executed one test successfully.

Both initial browser probes failed before the DOM fixture. Selenium and
Playwright reported `No usable sandbox!`; a separate namespace probe returned
`EPERM` for `unshare(CLONE_NEWUSER)`. The containers retained a non-root user,
read-only root, dropped capabilities, no-new-privileges, resource limits and
network isolation. Neither browser was retried with `--no-sandbox`.

These results qualify the two Python fixtures only. Installed browser packages
do not yet establish usable browser testing. Further sandbox qualification and
production activation require separate receipts.

A separate repair fixture exercised the real `DockerRunner` path. Initially,
`import flask` failed preflight with zero tests and the catalogue proposed
`flask==3.1.3`. After explicitly adding `Flask==3.1.3` to the fixture's
requirements, the registry-restricted installation completed in 1.801 seconds.
Two tests then passed without external networking, checking the Flask version,
a JSON POST and the absence of external connectivity. The networked installation
container received dependency manifests only, not project source. This is
mechanism evidence: no model chose the correction and no user project was run.

## Browser sandbox resolution

The qualified browser policy starts from the Docker 29.8.0 engine's upstream
seccomp profile (`moby/profiles`, `seccomp/v0.2.3`). Its explicit delta permits
only the namespace combinations and `chroot` needed by Chromium's own sandbox.
It does not enable other namespace types, add host capabilities or disable
AppArmor. The parent process still receives `EPERM` for `chroot` and creation
of a PID namespace outside the new user namespace.

The original 64-PID limit also produced an actual `pthread_create` resource
error. Only opted-in browser test containers use 256 PIDs; the 1 GiB memory,
two-CPU quota, read-only root, dropped capabilities, no-new-privileges and
external network isolation remain unchanged.

With that policy, both fresh browser sessions passed the four fixture checks:
empty initial storage, page title, click/DOM update and localStorage persistence
after reload. Selenium/Chromium 154 completed in 1.424 seconds and
Playwright/Chromium 153 in 0.987 seconds. These are fixture timings, not a
performance guarantee for generated applications.

Qualified seccomp SHA256:
`0f8bd34cf9980268f45c7f0a2a0d5dbff9559b3baf4d3cc4b464872e175a8c88`.
The profile applies to all processes in opted-in test containers, not just the
Chromium executable. The worker therefore leaves it disabled by default and
does not apply it to builds, inventory or dependency download containers.

The final source-aligned image is
`sha256:944801eb1120222a6b0ab29557d6d2cc10c8033c86b265a2c3f9b21cc5fa9fad`.
Its final integrated probes passed with `HOME=/tmp`: Selenium in 1.583 seconds
(153 peak PIDs/threads) and Playwright in 1.020 seconds (81 peak PIDs/threads).
Both retained seccomp filtering and `docker-default` AppArmor enforcement.
The final image layer only aligns trusted tool files and inventory; it does not
repeat package downloads or alter the selected browser binaries.

## Final DockerRunner and guarded activation

The final image also passed through the actual `DockerRunner` inside the
worker's bubblewrap command, using a private fixture rather than a production
job. The build retained the default seccomp policy and 64-PID limit. The
offline test used the verified private profile copy and 256-PID limit. One
pytest test passed in 1.45 seconds, covering nine assertions for sandbox
properties and Selenium page, click, storage and reload behavior. No model
chose or generated that fixture.

Source commit `00360f58d7f5f7b6dea45dd50226a9212c21ff9e` was pushed to
`origin/main`. The guarded lane activated only the project worker as
`project-dependencies-1ad81b1d7471c82e419c`, with the qualified image above and
`MONGARS_PROJECT_BROWSER_SANDBOX=1`. The API release, model settings, GPU
settings and generation deadlines were preserved. The lane retained a SQLite
backup and rollback path; no database restoration was needed.

The independent read-only check at 12:02 UTC confirmed six active services,
five online workers with heartbeat ages between 1.286 and 5.0 seconds, the new
worker process and release, the exact image and profile, and ten exact mounted
source files. All 52 protected data fingerprints matched the fresh baseline;
schema and worker registrations were unchanged and admission was idle. This
check did not create a project or execute an iPhone scenario.

Retained receipt identifiers:

- Fresh baseline SHA256:
  `9c52e9ee5bd3376f55b2f64a13d9c9a74fb496c1befb20250e3188b09da151bd`.
- DockerRunner qualification SHA256:
  `1930fbb298ea85bbd7d0f63d3f3e393e9c4796c214636896b5190b25967e9778`.
- Independent deployment receipt SHA256:
  `dee8ddb1a4100f5b360824a158d70e8e5ec9e6433165b66ca7566fa0693a5f19`.

## Limits

Static import analysis does not prove dynamic imports, browser startup, native
toolchains or complete application behavior. Automatic pytest plugin loading
remains disabled; installing pytest-asyncio alone does not enable async tests.
The operator recipes lock complete Python dependency closures. Generated
projects still do not persist their entire transitive installation closure as
a reusable project lock.

The deployed detection, structured feedback and isolated installation paths
are qualified separately. A complete agent-generated project in which the
model autonomously chooses a dependency correction and finishes successfully
has not yet been demonstrated by these receipts.

Private receipts are retained under the local SwarmerQualification directory:
`runtime-dependencies-20261001` and `browser-toolkit-20261001T092200`.
The repair fixture is recorded in `dependency-repair-flask-20261001T113126Z`;
its qualification receipt SHA256 is
`e92a7753e9e1465ebadf8447e7346f1fc6b81203321c466ce4dae9676d080e00`.
