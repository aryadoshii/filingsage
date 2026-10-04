# CLAUDE.md — FilingSage

FilingSage is an AI research analyst that watches a user's companies, ingests every new SEC filing, and delivers cited briefs and Q&A. It is Arya's flagship portfolio project with three purposes, in order:

1. A **working product a stranger can sign up for and use** — not a GitHub repo. It runs **fully on localhost** via one `docker compose up` (docs/decisions.md #30): the hosted deployment is paused under a hard zero-spend constraint, and returns only if a free host with enough RAM appears.
2. **Interview defensibility** — Arya must be able to explain every file, dependency, and architectural decision in detail. Code he can't defend is worthless here.
3. **Honest engineering** — every metric measured, never invented.

## Source of truth

`docs/filingsage-spec.md` is **frozen**. Execute it as written. Do not re-litigate its decisions — no Spark, Kafka/Redpanda, Kubernetes, knowledge graphs, billing, or new v1 data sources. If something in it appears genuinely broken, raise it explicitly as `Spec concern: <reasoning>` and then default to the spec.

## Honesty guardrails (absolute)

- Never write metric, benchmark, throughput, latency, or user-count numbers as placeholders — not in code comments, not in the README, not in docs. Placeholders leak.
- Numbers enter the README only when measured, reproducible, and dated. Perf claims only from load tests committed to the repo.
- Roadmap features are labeled roadmap, everywhere.

## Working rules

1. **One milestone at a time.** Follow the week plan in spec §16. Keep the "Current status" section below updated as pieces complete.
2. **Small, runnable increments.** After each piece: the exact command to run and the output that proves it works. Do not build on unverified pieces.
3. **Explain as you build.** Per component: 2–5 sentences on what it does, why this design, and what interview question it answers. At milestone completion, quiz Arya with 3–4 interview-style questions and correct his answers.
4. **Tests are not optional.** Every non-trivial module gets pytest coverage as it's written. Integration tests use testcontainers. Never defer tests.
5. **Every new dependency needs justification**: what problem it solves, why stdlib or an existing dep can't. Keep the tree lean.
6. **Maintain the Decisions Log.** Non-obvious choice → append a numbered entry to `docs/decisions.md` (rejected alternative + revisit threshold). The README keeps only a short summary.
7. **SEC EDGAR compliance:** declared `User-Agent` with contact email (`SEC_CONTACT_EMAIL` env var), ≤ 10 req/s, exponential backoff on 403/429. Never scrape anything against ToS.
8. **Git discipline:** conventional commits, suggest commit points as work lands, feature branches for larger pieces. History should show real iterative development.
9. **When Arya is stuck or demotivated:** find the smallest next shippable step. Never expand scope. Momentum beats perfection.
10. **Free tiers drift.** Before depending on an external free service, verify its current terms; propose the closest free alternative if they've changed.
11. **Git authorship:** commits must be authored solely by Arya. Never add
Co-Authored-By trailers, AI attribution lines, or any name other than Arya's to commits, PRs, or repository metadata — no exceptions.
12. **Zero spend.** No paid tiers, no paid infra, no "small" upgrades. Everything must run free on localhost; managed free tiers are optional extras, never requirements.
13. **Production is hands-off.** Never run migrations against a hosted database or deploy anything on Arya's behalf — he does that manually.

## Calibrating explanations

Arya is strong in: Python, FastAPI, LangGraph, hybrid RAG (retrieval fusion, cross-encoder reranking, NLI verification), JWT auth, pytest, Docker basics.
Teach more carefully: Terraform, Prometheus/Grafana/Loki, Celery at scale, Cloudflare Tunnel, Oracle Cloud, CI/CD beyond basics.
Machine: MacBook Air (Apple Silicon → every Compose image must have an arm64 variant).

## Layout

```
src/filingsage/
  api/          FastAPI app (main.py), read-only catalog routes, Redis rate limiter
  worker/       Celery app (+ beat schedule), tasks, recovery tool
  connectors/   SourceConnector ABC + EdgarConnector
  parsing/      bronze HTML -> sectioned silver Parquet (+ DQ checks)
  gold/         chunking, embedding, Qdrant store, retrieval, rerank, cited Q&A, question scoping
  financials/   XBRL companyfacts -> clean quarterly/annual series, statements, key stats
  db/           SQLAlchemy models, session, transactional event emitter
ui/             Streamlit dashboard (own image; HTTP client of the API only)
migrations/     Alembic
scripts/        up.sh (behind `make up`)
tests/          pytest (unit; testcontainers for integration)
docs/           filingsage-spec.md (frozen) · decisions.md (decision log)
deploy/         Fly.io configs for the paused hosted deployment
data/           local bronze/silver (gitignored)
```

## Commands

```bash
make up                                  # starts Docker if needed, builds + starts everything, opens http://localhost:8501
make down / make logs / make ps / make test
docker compose up --build -d             # what make up runs under the hood: migrate (one-shot), api, worker, beat, ui, postgres, redis, qdrant
docker compose ps                        # services healthy; migrate "exited (0)"; beat has no healthcheck
docker compose exec api python -m filingsage.cli ask "..." --ticker AAPL   # CLI inside the stack
curl localhost:8000/healthz              # liveness
pip install -e ".[dev]" && pytest        # tests (integration ones need Docker running)
ruff check src tests                     # lint
```

## Current status (update me)

**Done (verified end-to-end, hosted on Fly before the pause):**
- [x] Week 1 — Compose skeleton, `SourceConnector` + `EdgarConnector`, bronze → silver Parquet + DQ checks, Postgres schema + `events` outbox, scheduled ingestion, live HTTPS deploy
- [x] Week 2 — section-aware chunking, FastEmbed dense+sparse embeddings, Qdrant hybrid (RRF) retrieval, cross-encoder rerank, cited Q&A (`POST /qa`) with the zero-LLM-call "never bluff" gate, Redis rate limit, `recover-stale`
- ~~Terraform / Oracle VM~~ — superseded by Fly.io (decision #21), then by localhost-first (decision #30)

**Localhost v1 roadmap** — one increment at a time, each with tests + run/verify steps:

Phase 0 — local stack
- [x] L1 — local Qdrant in Compose, auto-migrate service, payload indexes in `ensure_collection()`, Celery beat replaces the GitHub cron, host-facing `.env.example`, `.dockerignore` (decisions #30, #31)
- [x] L1b — Streamlit dashboard at localhost:8501 (Ask with highlighted sources, Filings + track a company, live Pipeline) on new read-only API endpoints; Docker layer order fixed so code changes keep the baked-model cache (decision #32)

Product track — make it a finance research site people use (agreed build order, takes priority):
- [x] P1 — company research pages: XBRL financials (quarterly/annual, Q4 + cash-flow quarters derived and flagged), key stats, charts, statement + CSV, 8-K events timeline; companies overview home; question scoping; finance-research redesign; `make up` (decisions #33, #34)
- [ ] P2 — AI brief per filing (cited key points, numbers, anything unusual) + risk summary + event headlines; PDF report download (spec "briefs")
- [ ] P3 — accounts + personal watchlist home page (= L7 + L8 below)
- [ ] P4 — email alerts with the brief when a watched company files, to Mailpit locally (= L11)
- [ ] P5 — compare two companies; "what changed" vs the previous filing (spec v1.1 Change Detector)

Phase 1 — finish the RAG stack (spec §6)
- [ ] L2 — NLI claim verification (step 5): per-claim entailment score against cited chunks
- [ ] L3 — confidence gate (step 6): computed high/medium/low, flagged-unverified claims, one retrieval-expansion retry, honest fallback
- [ ] L4 — `qa_sessions` / `qa_messages` / `citations` tables; API returns resolved citations; endpoint to open the exact cited filing section
- [ ] L5 — semantic cache (step 1) in Redis, invalidated when a ticker gets new filings; `cache_hit` metric
- [ ] L6 — SSE streaming for Q&A (stage-by-stage progress for the UI)

Phase 2 — users & product backend (Week 3)
- [ ] L7 — real JWT auth (access + refresh), users, roles, per-user quotas (3 tickers, N questions/day)
- [ ] L8 — watchlist CRUD, filings feed, company search; beat ingests the union of watchlists
- [ ] L9 — LangGraph graph: planner → FilingAgent + FinancialsAgent (EDGAR XBRL) → verifier → confidence gate (retry ≤1) → composer; typed `AgentState`; Postgres checkpointing
- [ ] L10 — provider routing with per-provider budgets, retries/backoff, routing-share metric
- [ ] L11 — email briefs on new filings (`briefs` table, `brief.generated` → `alert.sent`), delivered to a local SMTP catcher (Mailpit)

Phase 3 — quality & observability (Week 4)
- [ ] L12 — golden dataset v1 (hand-labeled by Arya) + eval harness (hit@k, context precision/recall, citation accuracy, NLI faithfulness) + `eval_runs`; README results table naive → hybrid → +rerank → +verification
- [ ] L13 — Prometheus metrics (system + AI) + Grafana in Compose with a provisioned dashboard; admin stats endpoints
- [ ] L14 — CI on PRs: ruff, pytest (testcontainers), retrieval eval gate, docker build
- [ ] L15 — Locust load-test baseline on localhost, config committed, numbers dated

Phase 4 — frontend (Week 5)
- [ ] L16 — Next.js app, production-grade design: landing, auth, watchlist dashboard, filing feed with brief cards, streaming chat with citation popovers → exact filing section, public /status, admin dashboard
- [ ] L17 — polish, demo video, README final pass

**Definition of done (localhost edition):** on a fresh `docker compose up`, a new user signs up at localhost, adds 3 tickers, asks a cited question, receives an email brief in Mailpit when one of their companies files — while Grafana shows it happening.

**Open small items:** `RERANK_SCORE_FLOOR` uncalibrated (needs eval data, L12) · `recover-stale` ignores intact-but-stuck filings · Fly worker machine still exists on the paused hosted deployment.
