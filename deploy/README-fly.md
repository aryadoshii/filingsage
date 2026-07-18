# Fly.io deploy — one-time manual setup

Replaces the earlier Oracle/Terraform plan: Oracle's free-tier ARM capacity
wasn't available in the Mumbai region, and Fly's free allowance doesn't have
that capacity roulette or the spin-down-on-idle behavior of some other free
PaaS tiers. Neon (Postgres) and Upstash (Redis) are unchanged — same managed
services, different compute host.

Fly's model is one app = one process from one image, so the API and the
Celery worker are **two separate Fly apps** built from the same repo-root
`Dockerfile`:

| App | Config | Role | Region |
|---|---|---|---|
| `filingsage-api` | `fly.toml` (repo root) | uvicorn, public via Fly's proxy | `bom` (Mumbai) |
| `filingsage-worker` | `deploy/worker/fly.toml` | Celery worker, no public ingress | `sin` (Singapore) |

The two apps are in **different** regions, not a typo: `bom`'s Always Free
capacity was available for the api app but proved constrained specifically
for volume-attached machines, which the worker is (it mounts a Fly Volume
for `/app/data` — step 2 below). `sin` had capacity. See README.md →
Technical Decisions #21.

This file is a checklist for Arya to run by hand — nothing here runs itself,
and app creation should happen once, deliberately. Requires the Neon and
Upstash URLs to exist first.

## 0. Prerequisites

- [ ] `flyctl` installed and logged in (`fly auth login`)
- [ ] Neon Postgres connection string on hand (with `?sslmode=require`)
- [ ] Upstash Redis connection string on hand (`rediss://...`, TLS scheme)
- [ ] A freshly generated `INGEST_TOKEN` for prod (do **not** reuse the dev
      `.env`'s token): `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- [ ] Confirm both regions: `fly platform regions` — `fly.toml` (api) assumes
      `bom`, `deploy/worker/fly.toml` assumes `sin` (bom's Always Free
      capacity proved constrained for volume-attached machines specifically —
      see the table above). If capacity has shifted since, adjust
      `primary_region` in the relevant file before continuing; the two apps
      don't need to match each other.

## 1. Create both apps

```bash
fly apps create filingsage-api
fly apps create filingsage-worker
```

## 2. Create the worker's persistent volume

Bronze/silver land on disk until the R2 increment — a Fly Volume survives
redeploys and machine replacement; the machine's own local disk does not.

```bash
fly volumes create data --app filingsage-worker --region sin --size 1
```

(`--region` must match the worker app's `primary_region` in
`deploy/worker/fly.toml` — a volume is pinned to the region it's created in,
and a machine can only mount a volume in its own region. `--size` is in GB;
1 is the minimum and enough for the current filing volumes — resize later
with `fly volumes extend` if needed.)

## 3. Set secrets on each app

Same four vars on both apps (worker doesn't serve `/internal/ingest`, but it
needs DB/Redis/SEC contact too; INGEST_TOKEN is harmless there and keeps the
two apps' secret sets identical, which is one less thing to keep in sync):

```bash
for APP in filingsage-api filingsage-worker; do
  fly secrets set \
    DATABASE_URL="postgresql+psycopg://user:password@...neon.tech/filingsage?sslmode=require" \
    REDIS_URL="rediss://default:password@....upstash.io:6379" \
    SEC_CONTACT_EMAIL="you@example.com" \
    INGEST_TOKEN="<the fresh prod token from step 0>" \
    --app "$APP"
done
```

(`fly secrets set` triggers a release/restart on its own — no separate
deploy needed just for secrets, but the first deploy in step 5 still has to
happen at least once to actually get an image running.)

## 4. Run database migrations against Neon

**Not yet automated — a real gap, hit once already.** A fresh Neon project
has no schema; nothing in this checklist or in either fly.toml runs `alembic
upgrade head` for you. Skipping this step means the api/worker apps deploy
and pass health checks fine, then fail with a Postgres `UndefinedTable`
error the moment anything actually touches the `companies`/`filings`/
`events` tables (exactly what happened on the first real production ingest).
Run it by hand, from the repo root, against the real Neon URL:

```bash
DATABASE_URL="postgresql+psycopg://user:password@...neon.tech/filingsage?sslmode=require" \
  alembic upgrade head
```

Re-run after every migration-adding change, before redeploying. Candidates
for automating this later: a Fly `release_command` (runs once per deploy,
before the new version takes traffic) or a one-off GitHub Actions job
triggered on migration file changes. See README.md → Technical Decisions #22.

## 5. First deploy of each app

```bash
fly deploy --config fly.toml                     # filingsage-api (repo root)
fly deploy --config deploy/worker/fly.toml        # filingsage-worker
```

Validate config syntax before deploying if you want a sanity check first:

```bash
fly config validate --config fly.toml
fly config validate --config deploy/worker/fly.toml
```

## 6. Verify

```bash
fly status --app filingsage-api
fly status --app filingsage-worker
curl -sf https://filingsage-api.fly.dev/healthz
fly logs --app filingsage-worker   # confirm Celery connected to Redis/Postgres
```

## 7. Point the ingest cron at it

`.github/workflows/ingest-cron.yml` reads `secrets.INGEST_URL` and
`secrets.INGEST_TOKEN` from the GitHub repo's Actions secrets — set those in
GitHub (Settings → Secrets and variables → Actions), not here:

- `INGEST_URL` = `https://filingsage-api.fly.dev`
- `INGEST_TOKEN` = the same value set on the Fly apps in step 3

## Redeploying after code changes

```bash
fly deploy --config fly.toml
fly deploy --config deploy/worker/fly.toml
```

Both read the same Dockerfile, so a dependency change only needs to be
rebuilt once per app (Fly still builds separately per app — there's no
shared image cache across apps on the free tier).
