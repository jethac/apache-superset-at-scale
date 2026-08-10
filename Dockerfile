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
    SCOPE_PATH=/app/scope.yaml

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin scoreboard \
    && mkdir -p /data \
    && chown -R scoreboard:scoreboard /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root scope.yaml ./scope.yaml

USER scoreboard
EXPOSE 8000
VOLUME ["/data"]

# No secrets are baked in: credentials arrive only as runtime environment
# variables, so they never enter an image layer.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["scoreboard"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
