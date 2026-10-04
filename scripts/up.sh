#!/usr/bin/env bash
# One command from zero to a running FilingSage: `make up`.
#   1. checks .env exists (and fills in an INGEST_TOKEN if it's blank)
#   2. starts Docker Desktop if it isn't running (macOS)
#   3. builds and starts the whole stack, waits until the dashboard answers
#   4. kicks off one ingestion run, so new filings and missing financials
#      load now instead of at the next scheduled run (every 2h)
#   5. opens the dashboard in the browser
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://localhost:8501"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Set SEC_CONTACT_EMAIL (required by the SEC) and GROQ_API_KEY/GEMINI_API_KEY in .env, then run 'make up' again."
  exit 1
fi

# The dashboard's "Track a company" form needs INGEST_TOKEN. Generate one if
# the line is missing or empty — a random local secret, never committed.
python3 - <<'PY'
import pathlib, re, secrets
env = pathlib.Path(".env")
text = env.read_text()
if not re.search(r"^INGEST_TOKEN=\S+", text, flags=re.M):
    token = secrets.token_urlsafe(32)
    if re.search(r"^INGEST_TOKEN=", text, flags=re.M):
        text = re.sub(r"^INGEST_TOKEN=.*$", f"INGEST_TOKEN={token}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\nINGEST_TOKEN={token}\n"
    env.write_text(text)
    print("Generated an INGEST_TOKEN in .env.")
PY

if ! docker info >/dev/null 2>&1; then
  say "Starting Docker Desktop"
  if [[ "$(uname)" == "Darwin" ]]; then open -a Docker; fi
  for _ in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || { echo "Docker didn't start. Open Docker Desktop and retry."; exit 1; }
fi

say "Building and starting the stack (first build downloads the AI models; later builds are quick)"
docker compose up -d --build

say "Waiting for the dashboard"
for _ in $(seq 1 120); do
  curl -sf "$URL/_stcore/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -sf "$URL/_stcore/health" >/dev/null 2>&1 || {
  echo "The dashboard didn't come up. See: docker compose ps   and   docker compose logs ui api"
  exit 1
}

say "Checking EDGAR for new filings and loading any missing financials"
docker compose exec -T worker celery -A filingsage.worker.celery_app:celery_app \
  call filingsage.scheduled_ingest >/dev/null || echo "(couldn't queue the ingestion run; it will happen on schedule)"

say "FilingSage is running at $URL"
if command -v open >/dev/null 2>&1; then open "$URL"; fi
