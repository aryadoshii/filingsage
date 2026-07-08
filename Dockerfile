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
# layer-split caching gymnastics for now; the prod image gets its own
# hardening pass (non-root user, pinned lockfile) in Week 1.
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "filingsage.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
