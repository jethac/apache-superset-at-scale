# Base image pinned by digest: a tag alone is mutable and is the cheapest
# supply-chain attack there is.
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Hash-pinned transitive closure. --require-hashes makes pip refuse anything
# whose artefact does not match, so a compromised index cannot substitute a wheel.
COPY requirements.txt requirements-build.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps -r requirements.txt \
    && python -m venv /opt/build \
    && /opt/build/bin/pip install --require-hashes --no-deps -r requirements-build.txt

COPY pyproject.toml README.md ./
COPY src ./src
# Build the wheel offline from the already-verified build backend, then install
# it without touching an index.
RUN /opt/build/bin/python -m hatchling build -t wheel \
    && /opt/venv/bin/pip install --no-deps --no-index dist/*.whl


FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/facts.db \
    SCOPE_PATH=/app/scope.yaml \
    POLICY_PATH=/app/policy.yaml

# git is a runtime dependency, not a convenience: `backfill` measures historical commits in
# throwaway worktrees and `measure` records the commit it measured, so without it the debt series
# cannot be produced at all. Installed from the base image's own signed Debian suite rather than a
# third-party repository, with recommends off and the lists dropped in the same layer.
#
# It is here so the debt commands run inside this container. The alternative — a Python and Node
# toolchain on the host — is how they used to run, and it put the linter next to the deployment's
# credentials for no reason: measuring lint violations needs a checkout and nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin scoreboard \
    && mkdir -p /data \
    && chown -R scoreboard:scoreboard /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root scope.yaml ./scope.yaml
COPY --chown=root:root policy.yaml ./policy.yaml
# The simulator seeds the trend series from these; without them `simulate` in the
# container would silently produce a page with no debt history.
COPY --chown=root:root fixtures ./fixtures
ENV FIXTURES_PATH=/app/fixtures

USER scoreboard
EXPOSE 8000
VOLUME ["/data"]

# No secrets are baked in: credentials arrive only as runtime environment
# variables, so they never enter an image layer.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["scoreboard"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
