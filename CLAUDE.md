# CLAUDE.md — FilingSage

FilingSage is an AI research analyst that watches a user's companies, ingests every new SEC filing, and delivers cited briefs and Q&A. It is Arya's flagship portfolio project with three purposes, in order:

1. A **live, deployed SaaS** a stranger can sign up for and use — not a GitHub repo.
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
6. **Maintain the Decisions Log.** Non-obvious choice → append a numbered entry to README → Technical Decisions (rejected alternative + revisit threshold).
7. **SEC EDGAR compliance:** declared `User-Agent` with contact email (`SEC_CONTACT_EMAIL` env var), ≤ 10 req/s, exponential backoff on 403/429. Never scrape anything against ToS.
8. **Git discipline:** conventional commits, suggest commit points as work lands, feature branches for larger pieces. History should show real iterative development.
9. **When Arya is stuck or demotivated:** find the smallest next shippable step. Never expand scope. Momentum beats perfection.
10. **Free tiers drift.** Before depending on an external free service, verify its current terms; propose the closest free alternative if they've changed.
11. **Git authorship:** commits must be authored solely by Arya. Never add
Co-Authored-By trailers, AI attribution lines, or any name other than Arya's to commits, PRs, or repository metadata — no exceptions.

## Calibrating explanations

Arya is strong in: Python, FastAPI, LangGraph, hybrid RAG (retrieval fusion, cross-encoder reranking, NLI verification), JWT auth, pytest, Docker basics.
Teach more carefully: Terraform, Prometheus/Grafana/Loki, Celery at scale, Cloudflare Tunnel, Oracle Cloud, CI/CD beyond basics.
Machine: MacBook Air (Apple Silicon → arm64 images, matching the Oracle ARM VM target).

## Layout

```
src/filingsage/
  api/          FastAPI app (main.py)
  worker/       Celery app + tasks
  connectors/   SourceConnector ABC + EdgarConnector
tests/          pytest (unit; testcontainers for integration)
docs/           filingsage-spec.md (frozen)
data/           local bronze/silver in dev (gitignored; R2 in prod)
```

## Commands

```bash
cp .env.example .env                     # once; set SEC_CONTACT_EMAIL
docker compose up --build                # full dev stack
docker compose ps                        # all services should be healthy
curl localhost:8000/healthz              # liveness
pip install -e ".[dev]" && pytest        # host-side unit tests
ruff check src tests                     # lint
```

## Current status (update me)

- [ ] Day 1 — repo scaffold + Compose skeleton (api/worker/postgres/redis, healthchecks green, ping round-trip verified)
- [ ] Day 1 — `SourceConnector` ABC + `EdgarConnector.discover()` for 3 tickers → local bronze
- [ ] Day 1–3 — Oracle Cloud signup started (parallel; don't block on it)
- [ ] Week 1 — parse/section → silver Parquet + DQ checks; Postgres schema + `events`; Actions cron (10-ticker universe); Terraform; **live URL with HTTPS**
- [ ] Week 2 — chunking/embeddings/Qdrant hybrid + reranker; cited Q&A v0; minimal frontend; semantic cache
- [ ] Week 3 — LangGraph graph; provider routing; email briefs; real JWT auth + quotas
- [ ] Week 4 — observability full pass; golden dataset + eval CI gate; load-test baseline
- [ ] Week 5 — frontend polish; public /status; README results table; demo video
- [ ] Week 6 — buffer + first real users; feedback log; v1.1 design doc
