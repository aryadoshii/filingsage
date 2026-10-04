# Single image shared by the API and the Celery worker: same code, same
# dependencies, different command (set in docker-compose.yml). A second image
# would add build/maintenance cost with zero isolation benefit at this scale.
# No separate model sidecar: the spec's original self-hosted-on-a-24GB-VM
# plan (with a torch-based sidecar image) was superseded when that VM never
# materialized (README → Technical Decisions #21, #23) — embeddings AND
# reranking now run via FastEmbed (ONNX, no torch) in-process, in whichever
# of these two apps actually needs them (embeddings: both; rerank: API only
# — see gold/rerank.py).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Layer order is chosen for the cache: dependencies, then the baked
# models, and only THEN the source code. Docker rebuilds every layer
# after the first one whose inputs changed, so with src/ copied first (the
# original order), any code change that triggered a rebuild re-downloaded
# every model — the first local build spent most of its time on that step.
# Now the model layer depends only on pyproject.toml.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Bake the FastEmbed models into the image at build time (Technical
# Decisions #23, Option A) — no runtime download, so neither app has a
# cold-start dependency on the HF Hub being reachable, and first-call
# latency doesn't include a fetch. FASTEMBED_CACHE_PATH pins the cache to a
# known path in the image; fastembed otherwise defaults to the OS tempdir,
# which isn't something we want to depend on surviving or being writable by
# the non-root user set up below. Runtime code doesn't pass cache_dir
# explicitly, so it resolves to this same path via the env var — one cache
# location, populated once, at build time.
#
# HF_HOME does the same for the chunking tokenizer (gold/chunking.py loads
# tokenizer.json through huggingface_hub, which caches under HF_HOME) — it
# used to download on first use inside each container.
#
# This is ONE image shared by both apps (see file header) — baking the
# reranker here means filingsage-worker's image also carries it on disk,
# even though the worker never imports gold/rerank.py or loads it into
# memory (rerank runs in the API process only — see gold/rerank.py's
# module docstring for why). That's a build-size cost, not a runtime memory
# one: the lazy @lru_cache singleton in rerank.py only loads the model into
# a process that actually calls rerank(), and the worker never does.
# Splitting into two images to avoid that build-size cost was already
# rejected on its own terms (decision #9) and isn't reopened here.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed_cache \
    HF_HOME=/app/.hf_cache
RUN python -c "\
from fastembed import TextEmbedding, SparseTextEmbedding; \
from fastembed.rerank.cross_encoder import TextCrossEncoder; \
from huggingface_hub import hf_hub_download; \
TextEmbedding('BAAI/bge-small-en-v1.5'); \
SparseTextEmbedding('Qdrant/bm25'); \
TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2'); \
hf_hub_download('BAAI/bge-small-en-v1.5', 'tokenizer.json')"

COPY src/ ./src/
# Migrations ship in the image so the schema can be applied from inside the
# stack (the one-shot `migrate` Compose service) — no host-side Python needed
# just to create tables.
COPY alembic.ini ./
COPY migrations/ ./migrations/
# Dependencies are already installed above; this only registers the package
# itself (editable, so the Compose bind mount of src/ is what actually runs).
RUN pip install --no-cache-dir --no-deps -e .

# Non-root: root-in-container is one less privilege an escaped process would
# have, and it's what silences Celery's "running as superuser" warning.
# /app/data and the FastEmbed cache above are both created (and owned by
# `app`) before USER switches — the worker writes bronze/silver Parquet to
# the former and reads the baked models from the latter, and both need to
# be readable/writable by the user that actually runs the process.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "filingsage.api.main:app", "--host", "0.0.0.0", "--port", "8000"]