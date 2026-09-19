# Playwright's own image: the tag must match the pinned playwright version in
# dashboard/pyproject.toml (currently 1.62.0), otherwise the bundled browser
# and the Python client disagree.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

# uv resolves dashboard/uv.lock exactly (--frozen), so a rebuild months from
# now installs the same versions the tests ran against. 0.7.19 wrote the lock.
COPY --from=ghcr.io/astral-sh/uv:0.7.19 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/dashboard/.venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    FIXTURES_DIR=/app/fixtures \
    DATA_DIR=/data

WORKDIR /app/dashboard

# Lock and metadata first so a code change does not re-resolve the environment.
COPY dashboard/pyproject.toml dashboard/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY dashboard/app ./app
COPY fixtures /app/fixtures
ENV PATH=/app/dashboard/.venv/bin:$PATH

# Only matters for a plain `docker run` with no bind mount; under compose the
# host directory's ownership wins over this.
RUN mkdir -p /data && chown pwuser:pwuser /data

# Chromium and fontconfig write under HOME; /tmp is writable by any uid
# compose maps this container's user to.
ENV HOME=/tmp XDG_CACHE_HOME=/tmp

# uid 1001 in this base image.
USER pwuser

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

# One worker only: HubIdentity.claim() (app/hub_config.py) serializes
# concurrent POST /setup with an in-process asyncio.Lock, which only holds
# across a single worker process. A second worker would let two claims race
# past each other at the filesystem. Scale by running one container, not by
# adding --workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
