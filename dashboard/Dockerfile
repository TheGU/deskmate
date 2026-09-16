# Playwright's own image: the tag must match the playwright version in
# pyproject.toml (currently 1.62.0), otherwise the bundled browser and the
# Python client disagree.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FIXTURES_DIR=/app/fixtures \
    DATA_DIR=/data

WORKDIR /app

# Dependencies first so a code change does not re-resolve the environment.
COPY dashboard/pyproject.toml /app/dashboard/pyproject.toml
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.34" \
        "jinja2>=3.1" \
        "playwright==1.62.0" \
        "pillow>=11.0" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "httpx>=0.28" \
        "icalendar>=6.1" \
        "python-dateutil>=2.9" \
        "python-multipart>=0.0.32"

COPY dashboard/app /app/dashboard/app
COPY fixtures /app/fixtures

RUN mkdir -p /data /app/output

WORKDIR /app/dashboard
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

# One worker only: HubIdentity.claim() (app/hub_config.py) serializes
# concurrent POST /setup with an in-process asyncio.Lock, which only holds
# across a single worker process. A second worker would let two claims race
# past each other at the filesystem. Scale by running one container, not by
# adding --workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
