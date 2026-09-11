# Operator-built runtime. Both upstream images are pinned to immutable registry digests.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS node
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && python -m pip install --no-cache-dir pytest==8.4.2 ruff==0.12.12 \
    && mkdir -p /opt/swarmer /workspace /dependencies
COPY check_harness.py registry_proxy.py /opt/swarmer/
RUN printf '[pytest]\n' > /opt/swarmer/pytest.ini \
    && chmod -R a-w /opt/swarmer
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/tmp
WORKDIR /workspace
USER 65532:65532
