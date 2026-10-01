# Dependency recipes and actual runtime capabilities

Swarmer has separate environments. Installing a library on the Ubuntu host or
in the API virtualenv does not make it available to generated-project checks.
The project worker transports model requests using system Python; project code
runs in a disposable Docker container pinned by its immutable image ID.

## Supported boundaries

| Environment | Source of dependency identity | Qualification |
| --- | --- | --- |
| Project Python/Node checks | Docker base digests, `runtime-tools.lock`, actual image ID | Trusted harness inventory and real isolated checks |
| Browser test candidate | `runtime-browser.lock`, browser/system manifest, candidate image ID | Fresh Selenium and Playwright browser sessions inside the actual sandbox |
| Optional Python web application recipe | `runtime-python-web.in` and full hash-locked closure | Candidate recipe; each generated application still needs tests |
| Control-plane API | `server/pyproject.toml`, immutable release virtualenv and release receipt | API health, optional capability probes, dependency inventory |
| Mobile Expo application | `mobile/package-lock.json`, exact Expo/React Native pairing | Local CLI compatibility checks, native build and physical-device tests |
| Swift/iOS generated code | Dedicated macOS/Xcode worker | Ubuntu Python/npm checks cannot qualify it |
| Documents, OCR, embeddings and models | Separate optional worker/model environments | Package installation does not supply model weights or prove inference |

The new recipes do not install arbitrary libraries globally. They do not enable
an unavailable service or change an inference model. Browser sandbox policy is
qualified separately from package installation.
Large data-science, GPU, OCR and native toolchains belong in separate profiles.
Adding every possible package to a single environment would prevent reliable
version resolution and would make failures harder to reproduce.

## Reviewed recipes

`workers/project-worker/dependency-catalog.json` maps known Python import names
to exact distribution/version candidates. It deliberately distinguishes `PIL`
from `pillow`, `bs4` from `beautifulsoup4`, and `yaml` from `pyyaml`. Unknown
imports do not acquire an invented PyPI package name.

* `runtime-tools.lock`: pytest, Ruff, uv and their locked dependencies.
* `runtime-browser.lock`: Selenium, Playwright, pytest-asyncio, Hypothesis and
  their complete resolved Python dependency closure, constrained by the core lock.
* `runtime-python-web.lock`: Flask, FastAPI, HTTPX, Requests, Beautiful Soup,
  Pillow, Pydantic, PyYAML and their resolved closure. This is an optional
  application recipe, not an API upgrade or an instruction to use every package.

The current harness deliberately disables automatic pytest plugins.
`pytest-asyncio` being present in the candidate recipe does not enable async
test execution: the runtime profile reports `async_tests=unsupported` until a
trusted plugin integration is independently qualified. Do not silently skip
async tests or advertise this package as a working async test capability.

The first resolution targets Python 3.12 on Linux x86_64 (glibc 2.36). Hashes
may include additional wheels published for the same version; that does not
qualify those other architectures. Only wheels are installable. Direct pins
alone do not lock transitive dependencies, which is why full lockfiles are kept.

To reproduce a reviewed Python recipe in a prepared isolated environment:

```sh
python -m pip install --require-hashes --only-binary=:all: -r runtime-tools.lock
python -m pip install --require-hashes --only-binary=:all: -r runtime-browser.lock
python -m pip check
```

To propose an update, resolve the `.in` file with `uv pip compile`, Python 3.12,
`--python-platform x86_64-manylinux_2_36`, `--only-binary :all:`,
`--generate-hashes` and constraints from the other reviewed locks. Review the
diff, qualify a new immutable image, and retain the predecessor for rollback.
Do not run an unbounded upgrade inside an active worker.

## Autonomous dependency correction

1. The harness inspects Python imports without executing project code. Standard
   library, local modules and installed packages are considered separately.
2. Missing mandatory imports produce bounded structured diagnostics before
   pytest collection. Guarded optional imports are not treated as required.
3. A known import includes the exact catalogue recipe as a candidate; an unknown
   one remains unresolved. Existing project version pins are not overwritten.
4. The agent can add the dependency to `requirements.txt` in its next normal,
   recorded correction. Installation uses the existing registry-restricted stage,
   inside the isolated project dependency directory. It consumes the normal job
   budget; there is no hidden model retry or host installation.
5. The failed check runs again. An import passing does not prove browser startup,
   application functionality, native compilation or correct package selection.

The static scan is intentionally not a complete Python interpreter: dynamic
imports and runtime-selected dependencies may still fail during execution. It
does not evaluate arbitrary import guards, infer an unknown distribution name,
or install a package just because an exception mentions it. Repeated unresolved
failures retain their reason rather than becoming successful check receipts.

Generated projects still use their own exact dependency manifests through the
existing registry policy. Their entire transitive closure is **not** currently
persisted as a reusable project lock. The operator recipes are hash-locked;
do not describe every generated dependency installation as fully reproducible.

## Browser capability is more than a Python package

Selenium needs a browser and a matching driver. Playwright needs the browser
revision associated with its package and the operating-system libraries needed
to launch it. Prepare them during image construction, not in an untrusted test.
Use explicit executable paths and fresh profiles. Do not attach to an operator's
browser or allow an implicit download during a check.

The candidate must pass a real local fixture exercising JavaScript, clicks,
DOM changes and localStorage after a reload. Run as a non-root user, with a
read-only root, dropped capabilities, no-new-privileges, resource limits and
external networking disabled. A browser sandbox error remains a blocked
capability; do not silently add `--no-sandbox` to obtain a green result.

The browser runtime uses an explicit operator configuration,
`MONGARS_PROJECT_BROWSER_SANDBOX=1`, disabled by default. Its versioned seccomp
profile is derived from the exact Docker engine's upstream profile and permits
the additional operations needed to create Chromium's internal sandbox. The
worker verifies the profile hash and copies it into private job storage. Only
Python and Node test containers receive this profile; inventory, builds,
dependency installation and registry proxies retain the default policy.
No container receives additional host capabilities. AppArmor, no-new-privileges,
read-only roots, resource limits and external network isolation remain enabled.
The profile and its upstream revision must be reviewed when the Docker engine
changes; a browser test receipt is bound to the image and profile hashes.

On the qualified Linux host, prepare a separate candidate without touching the
active service (the output directory must not already exist):

```sh
python3 workers/project-worker/setup_browser_runtime.py build --output /path/to/new-private-candidate
```

The builder checks the existing base image ID, uses the signed Debian snapshot
recorded in `browser-system.lock.json`, builds an immutable image, records the
OS/Python/browser inventory and runs both real probes. Exit 2 means the candidate
failed qualification. It must not be activated on that result. Browser packages
and OS updates must be refreshed through a new reviewed snapshot; a frozen
snapshot is reproducible input, not a promise of perpetual security currency.

The capability inventory reports installed packages and discovered binaries.
It does not claim successful browser execution. Preserve the separate execution
receipt and the exact image ID that produced it.

## Continuous drift checks

```sh
python3 scripts/audit_dependency_catalog.py
```

This read-only check rejects catalogue/lock disagreement, conflicting recipe
versions, missing SHA256 locks and mobile manifest/lock-root disagreement. It
runs from `scripts/check.sh`. Its `scope=declarations_only` result must not be
reported as runtime qualification. TypeScript is explicitly pinned to the
already locked 5.9.3 instead of the floating `latest` tag.

For Expo, use the locally installed CLI with `CI=1` and `install --check`, then
Expo Doctor and the normal native build gates. Do not use an implicit `npx`
download or automatically migrate SDK/React Native to a new version.

Primary documentation: [Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/),
[Docker seccomp](https://docs.docker.com/engine/security/seccomp/),
[Playwright Python browsers](https://playwright.dev/python/docs/browsers),
[Playwright JavaScript browsers](https://playwright.dev/docs/browsers),
[Expo CLI](https://docs.expo.dev/more/expo-cli/),
[Expo SDK upgrades](https://docs.expo.dev/workflow/upgrading-expo-sdk-walkthrough/).
