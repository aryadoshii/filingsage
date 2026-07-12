# FilingSage

An AI research analyst that watches the companies you care about, reads every new SEC filing and piece of news the moment it drops, and tells you — with citations — what actually changed and whether it matters.

> **Guiding principle:** *Every major technology must justify its existence. If we can't answer "what business or engineering problem does this solve in our product?", it doesn't go into v1.*

**Status:** early development (Day 1 of the v1 build plan). This README grows with the system — every metric that ever appears here is measured, reproducible, and dated. No aspirational numbers.

## What it does (v1)

- Watchlist of tickers per user; scheduled ingestion of 10-K / 10-Q / 8-K filings from SEC EDGAR.
- Cited Q&A over your companies' filings — every claim mapped to the exact source section, verified by an NLI cross-encoder before it reaches you.
- Unprompted email briefs when a watched company files.
- Public [/status] page with live system and AI-quality metrics.
- A research tool, deliberately **not** investment advice.

## Architecture

*(diagram lands with the Week 1 deploy — see `docs/filingsage-spec.md` for the full design)*

## Running locally

```bash
cp .env.example .env       # then set SEC_CONTACT_EMAIL
docker compose up --build
curl localhost:8000/healthz
```

## Technical Decisions

Non-obvious choices, with reasoning. Each entry names the alternative we rejected and the threshold at which we'd revisit.

1. **DuckDB + Parquet over Spark/Iceberg** — data volume is single-digit GB; a distributed engine solves a problem we don't have. Revisit if silver-layer volume or query concurrency outgrows a single node.
2. **GitHub Actions cron over Airflow/Dagster** — three scheduled jobs don't justify an orchestrator stack. Migration criteria: job-dependency graph complexity, not job count.
3. **Qdrant hybrid (dense + sparse in one store) over a separate BM25 service** — one store, native fusion, less to operate.
4. **Celery + Redis over Kafka** — event-driven design with queue transport; every pipeline step emits an `events` row and chains the next task. Redpanda slots into this seam if event volume ever justifies it.
5. **Single VM + Docker Compose over Kubernetes** — k3s migration is deliberately Phase 3.
6. **Self-hosted embedding/rerank/NLI models on the 24 GB ARM VM over per-call APIs** — cost and latency control.
7. **US/EDGAR before India (NSE/BSE)** — official free API vs ToS-grey scraping; India connector is roadmap.
8. **Research tool, not advice** — product stance, regulatory hygiene, and consistent with prior quant research findings (IC ≈ 0).
9. **One container image for API and Celery worker** — same code and dependencies, different command. A second image adds build/maintenance cost with no isolation benefit at this scale; the model sidecar *will* be a separate image because its torch stack is heavy and orthogonal.
10. **No Postgres client library until the schema lands** — the API doesn't touch the database yet; dependencies enter the tree at the point of first use, and the dev stack's DB health is checked at the container level (`pg_isready`).
11. **Celery `task_acks_late` + `worker_prefetch_multiplier=1`** — at-least-once delivery for long-running ingestion tasks: a worker crash requeues the filing instead of losing it. Safe because pipeline tasks are idempotent (keyed by accession number; bronze writes are immutable).
12. **8-K sections use full item passthrough with curated canonical names** — unmapped items get generic keys with the document's own heading as the title. Quarantine strictly means parse failure, never scope exclusion, so the quarantine-rate metric stays honest. 10-K/10-Q keep a curated item set by design (their catalogs contain true boilerplate).
13. **Sync httpx client for ingestion, async for the API** — connectors run inside Celery prefork workers where async adds complexity without concurrency benefit at watchlist scale; httpx keeps one HTTP library across both worlds.
14. **Hand-rolled rate limiter and backoff over tenacity** — ~25 lines total, fully explainable, with SEC-specific behavior (403 as throttle signal, Retry-After honored, jitter). A retry library would be our first dependency we couldn't defend line-by-line.
15. **Discovery scope = EDGAR's `recent` submissions window (~1,000 filings/company)** — years of history, right-sized for a monitoring product; full-history backfill is a non-goal for v1.
16. **String status column + code validation over Postgres ENUM** — PG enum changes are DDL migrations; the pipeline's status vocabulary will grow, and a String(32) with a Python enum keeps that free.
17. **`events` as a lightweight transactional outbox** — `emit_event` joins the caller's transaction instead of committing, so state changes and their audit events are atomic. Events can never lie about what the database contains.
18. **psycopg3 as the single Postgres driver** — one driver serving sync (Celery workers) and async (FastAPI) from the same codebase; binary wheels for zero-compile installs on arm64.
19. **Dev Postgres runs only in Docker; native Postgres stopped** — a host-level Postgres on 5432 silently intercepts `localhost` connections ahead of Docker's port proxy. One database per port, and ours is the container.
20. **Atomic writes everywhere data lands (tmp + rename)** — bronze documents and silver Parquet both; a crash mid-write can never leave truncated output that a later run mistakes for valid data.

## Honesty

Throughput and latency claims on this page come from load tests committed to this repo, with the test configuration linked. User counts are real registered users. Roadmap features are labeled roadmap — here and in interviews.
