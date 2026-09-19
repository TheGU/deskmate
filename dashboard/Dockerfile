# Playwright's pip package version must match the pinned version in
# dashboard/pyproject.toml (currently 1.62.0): the browser build Playwright
# installs below (--only-shell chromium) has to agree with the Python client,
# or the bundled browser and the client disagree at launch time.
FROM python:3.12-slim-bookworm

# uv resolves dashboard/uv.lock exactly (--frozen), so a rebuild months from
# now installs the same versions the tests ran against. 0.7.19 wrote the lock.
COPY --from=ghcr.io/astral-sh/uv:0.7.19 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/dashboard/.venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # Kept at runtime too: the launcher looks for the headless shell here.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    FIXTURES_DIR=/app/fixtures \
    DATA_DIR=/data

WORKDIR /app/dashboard

# Lock and metadata first so a code change does not re-resolve the environment.
COPY dashboard/pyproject.toml dashboard/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY dashboard/app ./app
COPY fixtures /app/fixtures
ENV PATH=/app/dashboard/.venv/bin:$PATH

# --only-shell installs just the headless Chromium shell (no full Chrome UI
# build), which is what the launcher in render.py picks when running
# headless; --with-deps pulls in the OS libraries it needs as root, then the
# apt lists it leaves behind are cleaned up so they do not bloat the image.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

# uid/gid 1001 matches the PUID/PGID default in docker-compose.yml.
RUN groupadd -g 1001 hub && useradd -u 1001 -g 1001 -M hub

# Only matters for a plain `docker run` with no bind mount; under compose the
# host directory's ownership wins over this.
RUN mkdir -p /data && chown hub:hub /data

# Chromium and fontconfig write under HOME; /tmp is writable by any uid
# compose maps this container's user to.
ENV HOME=/tmp XDG_CACHE_HOME=/tmp

USER hub

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

# One worker only: HubIdentity.claim() (app/hub_config.py) serializes
# concurrent POST /setup with an in-process asyncio.Lock, which only holds
# across a single worker process. A second worker would let two claims race
# past each other at the filesystem. Scale by running one container, not by
# adding --workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
