# Single image shared by the API and the Celery worker: same code, same
# dependencies, different command (set in docker-compose.yml). A second image
# would add build/maintenance cost with zero isolation benefit at this scale.
# The model sidecar (BGE/reranker/NLI) WILL be a separate image later — its
# heavy torch dependencies are orthogonal to the app.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
# Editable install + the bind mount in compose = live-reload dev loop.
# Image rebuilds are only needed when dependencies change, so we skip
# layer-split caching gymnastics for now; a pinned lockfile is a later pass.
RUN pip install --no-cache-dir -e .

# Non-root: root-in-container is one less privilege an escaped process would
# have, and it's what silences Celery's "running as superuser" warning.
# /app/data is created (and owned by `app`) here, before USER switches —
# the worker writes bronze/silver Parquet there and needs it writable.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "filingsage.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
