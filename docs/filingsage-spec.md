# FilingSage — v1 Architecture & Build Plan

**One-liner:** An AI research analyst that watches the companies you care about, reads every new SEC filing and piece of news the moment it drops, and tells you — with citations — what actually changed and whether it matters.

**Guiding principle (goes in the README verbatim):** *Every major technology must justify its existence. If we can't answer "what business or engineering problem does this solve in our product?", it doesn't go into v1.*

---

## 1. Frozen v1 Scope

**In:** Connector abstraction (EDGAR first) · Watchlists · Scheduled automated ingestion · Hybrid RAG with citations · Verification layer · LangGraph agent orchestration · Email briefs on new filings · Cited Q&A · Auth + quotas · Production deployment · System + AI observability (admin/status page as a product surface) · CI/CD with eval gates.

**Deferred:** Change Detector (v1.1) · Timeline Intelligence (v2) · Airflow/Dagster (when job-dependency count justifies it) · India/NSE-BSE connector (roadmap) · Kafka/Redpanda (only if event volume ever justifies it) · Billing (never — quotas only).

**Out, with documented reasoning:** Spark, Iceberg (data volume is single-digit GB; DuckDB + Parquet wins at this scale) · Knowledge graph (solves no current problem) · Price prediction (deliberate product stance — research tool, not advice).

---

## 2. System Architecture

```
                        ┌─────────────────────────────────────────┐
                        │      GitHub Actions (cron, free)        │
                        │  ingestion runs every 2h · visible logs │
                        └───────────────────┬─────────────────────┘
                                            │ triggers
┌───────────────┐   ┌───────────────────────▼──────────────────────┐
│  SEC EDGAR    │──▶│  INGESTION SERVICE (Python, containerized)   │
│  (free API)   │   │  SourceConnector ABC → EdgarConnector        │
└───────────────┘   │  discover → fetch raw → parse → normalize    │
                    └───────┬──────────────────────┬───────────────┘
                            │ raw HTML/XBRL        │ clean Parquet
                    ┌───────▼────────┐    ┌────────▼────────┐
                    │ R2: bronze/    │    │ R2: silver/     │
                    │ (immutable raw)│    │ (Parquet, typed)│
                    └────────────────┘    └────────┬────────┘
                                                   │ chunk + embed (BGE on VM)
                            ┌──────────────────────▼───────────────────────┐
                            │  GOLD SERVING LAYER                          │
                            │  Qdrant Cloud: dense + sparse (hybrid)       │
                            │  Neon Postgres: metadata, events, users,     │
                            │  watchlists, briefs, citations, quotas       │
                            └──────────────────────┬───────────────────────┘
                                                   │
        ┌──────────────────────────────────────────▼──────────────────────┐
        │  ORACLE ALWAYS-FREE ARM VM (4 OCPU / 24 GB) — Docker Compose    │
        │  ┌────────────┐ ┌──────────────┐ ┌───────────────────────────┐ │
        │  │ FastAPI    │ │ Celery       │ │ Model sidecar:            │ │
        │  │ (async API)│ │ workers +    │ │ BGE-small embeddings +    │ │
        │  │ JWT auth   │ │ beat         │ │ bge-reranker + NLI x-enc  │ │
        │  └─────┬──────┘ └──────┬───────┘ └───────────────────────────┘ │
        │        │   Upstash Redis: queue broker · semantic cache ·      │
        │        │   rate limits                                          │
        └────────┼────────────────────────────────────────────────────────┘
                 │ HTTPS via Cloudflare Tunnel (free TLS, hidden IP)
   ┌─────────────▼─────────────┐        ┌────────────────────────────┐
   │ Frontend: React/Next on   │        │ Observability:             │
   │ Cloudflare Pages          │        │ Grafana Cloud (metrics +   │
   │ watchlist · chat · admin  │        │ Loki logs) · Sentry ·      │
   └───────────────────────────┘        │ UptimeRobot · /status page │
                                        └────────────────────────────┘
```

LLM inference: Groq (llama-3.3-70b) primary → Gemini Flash fallback, via a provider-routing layer with per-provider budgets, retries, and structured-output enforcement (Pydantic).

---

## 3. Event Flow (design-level event-driven, Celery transport)

Every pipeline step emits a row to an `events` table (doubles as audit trail) and triggers the next Celery task:

`filing.discovered → filing.fetched → filing.parsed → filing.embedded → analysis.completed → brief.generated → alert.sent`

Failures emit `*.failed` events with error payloads → surfaced on the admin page and in Grafana. This gives event-driven *architecture* without event-streaming *infrastructure*; Redpanda slots into this seam later if volume ever justifies it.

---

## 4. Data Model (core tables, Neon Postgres)

- `companies` (cik, ticker, name, sector)
- `filings` (id, cik, form_type, filed_at, accession_no, r2_bronze_key, r2_silver_key, status)
- `chunks` (id, filing_id, section, seq, text_hash, qdrant_point_id)
- `events` (id, type, entity_id, payload_json, created_at) — audit trail
- `users`, `watchlists`, `watchlist_items`, `quotas`
- `briefs` (id, filing_id, user_id, content, confidence, sent_at)
- `citations` (id, brief_or_answer_id, chunk_id, claim_text, verification_score)
- `qa_sessions`, `qa_messages` (with cited chunk ids + confidence per answer)
- `eval_runs` (id, git_sha, dataset_version, metrics_json, passed)

---

## 5. Ingestion / ETL Detail

**EDGAR specifics (all free, official):**
- Company universe: `company_tickers.json` (CIK↔ticker map).
- Discovery: `data.sec.gov/submissions/CIK##########.json` per watched company, polled on schedule; new accession numbers = `filing.discovered`.
- Fetch: filing index + primary document from `sec.gov/Archives`.
- **Compliance:** declared `User-Agent` with contact email, ≤10 req/s, backoff on 403/429. Document this in the README — respecting a data provider's fair-access policy is itself an interview point.
- v1 forms: 10-K, 10-Q, 8-K.

**Bronze → Silver → Gold:**
- Bronze: raw HTML/XBRL to R2, immutable, keyed by accession number.
- Silver: parsed + sectioned (Item 1A Risk Factors, Item 7 MD&A, etc.) → typed Parquet on R2. Data-quality checks here (non-empty sections, encoding, dedupe by text hash) — failures quarantine the filing and emit `filing.parse_failed`.
- Gold: section-aware chunking (respect item boundaries; ~512-token chunks, overlap) → BGE-small-en-v1.5 dense vectors + Qdrant/FastEmbed sparse (BM25-family) vectors → Qdrant, with metadata (cik, form, filed_at, section) for filtered retrieval. DuckDB reads silver Parquet directly for analytics and the admin stats page.

---

## 6. Retrieval & RAG Stack

1. **Semantic cache check** (Redis): embed query, cosine vs cached queries above threshold → serve cached answer with `cache_hit=true` metric.
2. **Hybrid retrieval** (Qdrant): dense + sparse, fused (RRF), filtered by user's watchlist/company/date scope. Top-40.
3. **Rerank**: bge-reranker cross-encoder on VM → top-8, with score floor (below floor → "insufficient evidence" path, never bluff).
4. **Generate**: provider-routed LLM, structured output = answer + claim list, each claim mapped to chunk ids.
5. **Verify**: NLI cross-encoder (you've run deberta-v3 NLI in EarningsEdge) checks each claim against its cited chunks → per-claim entailment score.
6. **Confidence gate**: aggregate rerank + verification scores → high: deliver with citations · medium: deliver with flagged-unverified claims · low: retrieval expansion retry once, else honest "can't support this from the corpus."

Every stage emits metrics (latency, scores, token cost) — this is the raw material for AI observability.

---

## 7. Agent Layer (LangGraph — a real graph, not a chain)

```
                 ┌──────────┐
     query/  ───▶│ Planner  │ decides scope: which companies, which docs, which sub-agents
     new filing  └────┬─────┘
                      │ (parallel fan-out)
      ┌───────────────┼────────────────┐
┌─────▼──────┐ ┌──────▼──────┐ ┌───────▼───────┐
│ FilingAgent│ │ NewsAgent   │ │ FinancialsAgent│   each does its own retrieval
└─────┬──────┘ └──────┬──────┘ └───────┬───────┘
      └───────────────┼────────────────┘
                ┌─────▼─────┐
                │ Verifier  │ NLI claim-checking across all agent outputs
                └─────┬─────┘
             ┌────────▼────────┐
             │ Confidence gate │──low──▶ retry w/ expanded retrieval (max 1) ──▶ honest fallback
             └────────┬────────┘
                 high │
             ┌────────▼────────┐
             │ Composer        │ brief / answer, citations stitched
             └────────┬────────┘
                      ▼
                Delivery (email via Composio pattern / in-app)
```

State: typed `AgentState` (Pydantic). Conditional edges on confidence. Retries with backoff on tool/LLM failures. Checkpointing to Postgres so runs are inspectable — every historical run becomes a debugging artifact and demo material.

---

## 8. API & Auth

FastAPI, fully async. JWT access+refresh (you built JWT+RBAC at Deepmindz — same pattern, simplified). Endpoints: auth, watchlist CRUD, filings feed, Q&A (SSE streaming), briefs history, admin/status. Rate limiting + quotas via Redis (free tier: 3 tickers, N questions/day) — this is the product's "plans" story without billing. OpenAPI docs public.

## 9. Frontend

Next.js on Cloudflare Pages. Pages: landing, auth, watchlist dashboard, filing feed w/ brief cards, chat with inline citation popovers (click → exact filing section), **public /status page** (uptime, filings ingested, eval scores) and admin dashboard (ingestion stats, agent failures, token spend, cache hit rate). Reuse the live-streaming/typewriter pattern from NewsletterAgent for agent progress.

## 10. Observability (system + AI)

- **System:** Prometheus metrics from FastAPI/Celery → Grafana Cloud; Loki for logs; Sentry for errors; UptimeRobot external probe.
- **AI (product surface):** retrieval precision on golden set, per-stage latency, confidence distribution, verification pass rate, hallucination-flag rate, token cost per brief/user/day, cache hit rate, per-provider routing share, agent failure counts. Shown on admin page; headline numbers on public /status.

## 11. Evaluation Pipeline (CI gate)

- Golden dataset: 50–100 Q&A pairs over a fixed set of ingested filings, hand-labeled with expected source sections. Grows over time; versioned in-repo.
- Metrics: retrieval hit@k, context precision/recall, answer faithfulness (RAGAS-style + your NLI verifier), citation accuracy.
- Runs in CI on every PR touching retrieval/agents; **regression beyond threshold blocks merge.** Results logged to `eval_runs` and MLflow (local, on VM).
- README gets a Results table with real before/after numbers as you tune (naive → hybrid → +rerank → +verification).

## 12. CI/CD (GitHub Actions)

PR: ruff + mypy → pytest (unit/integration, testcontainers for Postgres/Redis) → eval gate → docker build → Trivy scan.
Main: build + push GHCR → SSH deploy to VM (compose pull && up -d) → smoke tests against prod → Sentry release tag. Badges in README.

## 13. Infrastructure (all free)

| Layer | Service |
|---|---|
| Compute | Oracle Always Free ARM VM (4 OCPU/24GB), Docker Compose |
| IaC | Terraform: OCI (VM, VCN, security lists) + Cloudflare (DNS, tunnel). `prevent_destroy` on stateful resources; billing alerts day one |
| DB / Vectors / Cache / Objects | Neon · Qdrant Cloud 1GB · Upstash Redis · Cloudflare R2 |
| Scheduling | GitHub Actions cron (public repo) |
| Frontend / Ingress | Cloudflare Pages · Cloudflare Tunnel |
| Monitoring | Grafana Cloud · Sentry · UptimeRobot |
| LLMs | Groq + Gemini free tiers, provider routing |

Fallback if Oracle signup fails after ~3 attempts across regions: GCP e2-micro (always free) for API + move embeddings/rerank to hosted APIs; revisit Oracle later. Do not burn more than 2 days fighting Oracle capacity.

## 14. Technical Decisions Log (seed the README with these)

1. DuckDB+Parquet over Spark/Iceberg — data volume; documented migration threshold.
2. GitHub Actions cron over Airflow v1 — 3 jobs don't justify a scheduler stack; migration criteria documented.
3. Qdrant hybrid (dense+sparse in one store) over separate BM25 service — one store, native fusion, less to operate.
4. Celery+Redis over Kafka — event-driven design, queue transport; seam documented.
5. Single VM + Compose over Kubernetes — k3s migration is Phase 3, deliberately.
6. Self-hosted embedding/rerank/NLI models on 24GB ARM over per-call APIs — cost + latency control.
7. US/EDGAR before India — official free API vs ToS-grey scraping; NSE/BSE as roadmap connector.
8. Research tool, not advice — product stance, regulatory hygiene, and consistent with QuantEdge IC≈0 findings.

## 15. Honesty Guardrails (non-negotiable)

- Every metric in the README is measured, reproducible, and dated. No aspirational numbers presented as results.
- User counts are real registered users only. "Validated with feedback from N people" only if true.
- Throughput/latency claims come from load tests committed to the repo (Locust), with the test config linked.
- If a feature is roadmap, it is labeled roadmap — including in interviews.

## 16. Build Plan — 6 Weeks

**Days 1–3 (start today):**
1. Repo `filingsage` (public), README skeleton with the one-liner, principle, and Decisions log stubs.
2. Docker Compose skeleton: FastAPI hello-world + Postgres + Redis + Celery worker, healthchecks.
3. `SourceConnector` ABC + `EdgarConnector.discover()` against submissions API for 3 tickers — filings landing in local bronze.
4. Oracle account signup started in parallel (it can take days — don't block on it).

**Week 1:** EDGAR fetch/parse/section pipeline → silver Parquet + data-quality checks; Postgres schema + events table; Actions cron running ingestion for a 10-ticker default universe; Terraform for VM; **deploy the skeleton — live URL with HTTPS by end of week, even if it only shows ingestion stats.**
**Week 2:** Chunking + embeddings + Qdrant hybrid + reranker; cited Q&A endpoint v0 (no agents yet — straight RAG); minimal frontend (auth stub, chat, feed); semantic cache.
**Week 3:** LangGraph graph (planner/parallel agents/verifier/confidence gate); provider routing; email briefs wired to `filing.discovered` events; real JWT auth + quotas.
**Week 4:** Observability full pass (Prometheus/Grafana/Loki/Sentry + AI metrics + admin page); golden dataset v1 + eval harness + CI gate; load test baseline.
**Week 5:** Frontend polish, public /status page, README with architecture diagram + measured Results table + Decisions log; demo video; docs.
**Week 6:** Buffer (something will slip — plan for it) + first real users: your own daily use, then 5–10 people from your trading circle/batchmates; feedback log; v1.1 (Change Detector) design doc.

**Definition of done for v1:** a stranger can sign up at the live URL, add 3 tickers, ask a cited question, and receive an unprompted email brief when one of their companies files — while you watch it happen on Grafana.

## 17. Roadmap (post-v1)

v1.1 Change Detector (section-level diffs vs prior filing, "Lazy Prices" framing) → v1.5 news/RSS + transcript connectors, Airflow/Dagster migration if justified → v2 Timeline Intelligence (temporal evidence reconstruction) → Phase 3 k3s migration → NSE/BSE connector.
