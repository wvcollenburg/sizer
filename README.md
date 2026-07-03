# SC// Infrastructure Sizer

A web application that sizes hyperconverged (HCI) clusters from a customer's existing
VMware environment and produces branded, ready-to-send hardware proposals.

Point it at a **Live Optics** or **RVTools** export (or enter workload numbers by hand),
and it recommends how many SC// nodes are needed — CPU, RAM, storage, and IOPS — then
generates a PowerPoint deck, Word document, and PDF for the customer, in any of 15
languages.

---

## What it does

- **Imports real workload data** — parses Live Optics and RVTools `.xlsx` exports to
  derive total vCPUs, RAM, storage, utilized GHz, IOPS, and VM counts (including
  deduplication of shared datastores reported per-host).
- **Sizes a cluster** ([app/recommend.py](app/recommend.py)) — picks node counts and
  hardware to meet compute, memory, storage-capacity, and IOPS demand with N+1
  headroom. Compute is sized to an *active floor* that blends utilized GHz with CPU
  benchmark scores (SPECrate), not nameplate capacity, so recommendations track what a
  workload actually consumes.
  - **Two sizing modes** — *Certified* (fixed SC// appliance configurations) and
    *Validated* software-only (same models, disk counts trimmed to need within the
    supported flash band and per-cluster disk limits).
  - **Storage-only nodes** — for storage-heavy workloads, extra nodes join the storage
    cluster with no VMs (low CPU, minimal RAM) to add capacity and IOPS while keeping
    compute licensing down.
- **Generates deliverables** — branded PowerPoint proposal
  ([app/export_pptx.py](app/export_pptx.py)), Word proposal
  ([app/export_docx.py](app/export_docx.py)), and PDF conversions (via headless
  LibreOffice), including per-recommendation cluster network diagrams
  ([app/cluster_diagram.py](app/cluster_diagram.py)) and utilization/benchmark charts.
- **Speaks 15 languages** — the UI and every generated export are localized (EN, DE, FR,
  NL, ES, IT, PT, JA, SV, DA, NO, FI, ET, LV, LT), with CJK-capable fonts bundled for
  Japanese output.
- **Multi-tenant with accounts** — optional login, email-domain-based tenancy, roles, and
  saved/shared sizings, with a super-admin panel for editing the hardware catalog, CPU
  data, and sizing tunables live (no redeploy). Includes rate limiting, email
  verification, account lockout, and a daily GDPR retention/anonymization job.

## Architecture

| Layer | Technology |
|-------|-----------|
| Web framework | Flask 3 + gunicorn (2 workers) |
| Database | PostgreSQL 16 (SQLAlchemy ORM) |
| Rate-limit / shared state | Redis 7 |
| Exports | python-pptx, python-docx, cairosvg, headless LibreOffice |
| Frontend | Server-rendered Jinja templates + vanilla JS ([app/static/](app/static/)) |
| Packaging | Docker + Docker Compose |

Key modules under [app/](app/): [app.py](app/app.py) (routes/wiring),
[recommend.py](app/recommend.py) (sizing engine), [liveoptics.py](app/liveoptics.py) /
[rvtools.py](app/rvtools.py) (importers), [tunables.py](app/tunables.py) (admin-editable
sizing constants), [auth.py](app/auth.py) (accounts/tenancy), and
[i18n.py](app/i18n.py) (localization).

## Quick start (Docker Compose)

The Compose stack brings up the app, PostgreSQL, and Redis together. The database is
seeded automatically on first boot ([entrypoint.sh](entrypoint.sh)).

```bash
# 1. Configure environment
cp .env.template .env
#    then edit .env — at minimum set SECRET_KEY, and SUPER_ADMIN_EMAIL /
#    SUPER_ADMIN_PASSWORD to bootstrap the admin account.
#    Generate a secret with:  python -c "import secrets; print(secrets.token_hex(32))"

# 2. Build and run
docker compose up --build

# 3. Open the app
open http://localhost:5000
```

The super-admin account is created on boot from `.env`; sign in there to reach the admin
panel and edit the hardware catalog and sizing tunables.

> **Note:** JavaScript and template changes require a rebuild/recreate of the `sizer`
> image — there is no host bind-mount. Rebuild with `docker compose up --build`.

## Configuration

All configuration is via environment variables — see [.env.template](.env.template) for
the annotated list. The essentials:

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | **Required in production.** Signs session cookies; must be shared across gunicorn workers or logins won't persist. |
| `DATABASE_URL` | PostgreSQL connection string (defaults to the Compose `db` service). |
| `RATELIMIT_STORAGE_URI` | Rate-limit backend; point at Redis for exact cross-worker limits. |
| `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` | Bootstraps the super-admin account on boot (never overwrites an existing password). |
| `APP_BASE_URL` | Public base URL for links in verification/reset emails (avoids Host-header poisoning). |
| `SESSION_COOKIE_SECURE` | Set `true` when served over HTTPS. |
| `ENABLE_SCHEDULER` | Set `0` to disable the in-app daily retention/GDPR job (e.g. for CLI/one-off processes). |

## Deployment

The app is designed to run behind an nginx TLS-terminating reverse proxy (one trusted
proxy hop; `ProxyFix` reads `X-Forwarded-For`/`X-Forwarded-Proto`). In production set
`SECRET_KEY`, `APP_BASE_URL`, and `SESSION_COOKIE_SECURE=true`, and point
`RATELIMIT_STORAGE_URI` at Redis.

## Tests

```bash
python -m pytest tests/
```

The suite covers the compute-floor sizing model and the end-to-end perf-based sizing and
export paths ([tests/](tests/)). Auth/DB tests run on SQLite via `create_all`; note that
the full `seed.py` requires PostgreSQL.

## Adding a language

A new language means adding **both** the UI catalog (`app/static/js/lang/<code>.js`) and
the export catalog (`app/locales/<code>.json`) in the same change, plus registering the
code in [app/i18n.py](app/i18n.py). Never ship one without the other, or the UI and the
generated documents fall out of sync.
