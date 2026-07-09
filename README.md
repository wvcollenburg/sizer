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

## Features & functionalities

A full inventory of what the sizer does today.

### Data import & workload capture
- **Live Optics import** — parses the `.xlsx` assessment export; derives total vCPUs, RAM, storage (provisioned/used), utilized GHz, IOPS, and VM counts.
- **RVTools import** — the same, from an RVTools `.xlsx` export.
- **Shared-datastore deduplication** — datastores reported per-host under local names are de-duplicated by a capacity/used/free/VM-count signature, so shared storage isn't counted multiple times.
- **Manual entry** — key in workload numbers by hand when no assessment file is available.
- **Guided import wizard** — a stepped, walk-through import flow as the default experience, with the original single-page "classic" view kept as an advanced mode.
- **Upload hardening** — magic-byte sniffing rejects non-`.xlsx` files, and row/column caps stop decompression-bomb spreadsheets.

### Sizing engine
- **Full-stack sizing** — recommends node counts to satisfy compute, memory, storage-capacity, and IOPS demand simultaneously, with N+1 failure headroom.
- **Active compute floor** — compute is sized to what the workload actually consumes, blending utilized GHz with CPU benchmark scores (SPECrate) rather than nameplate core counts.
- **Two sizing modes** — *Certified* (fixed SC// appliance configurations) and *Validated* software-only (same models, disks trimmed to need within the supported flash band and per-cluster disk limits).
- **Storage-only nodes** — no-VM nodes that join the storage cluster to add capacity/IOPS without adding compute licensing.
- **Day-one consumption caps** — bound how full the cluster may be on day one (storage vs. full capacity, RAM vs. N-1), leaving planned growth headroom.
- **IOPS-aware** — sizes on per-drive IOPS, with replication-factor write-amplification treated as real demand.
- **Multi-site** — size several clusters in one session and roll them into a single combined proposal.
- **Determinant transparency** — each recommendation reports which resource (compute / RAM / storage / IOPS) drove the node count.

### CPU catalog & performance intelligence
- **625-CPU catalog** — make, generation, and model, with base / all-core / max clocks and P-/E-core counts.
- **Benchmark scores** — SPECrate and PassMark per CPU.
- **Benchmark autofill** — look up a CPU's performance data on demand (`/api/cpu-perf`) to feed the perf-based compute floor.

### Deliverables & exports
- **Branded PowerPoint proposal** — leads with the recommendation, fully branded.
- **Word (DOCX) proposal** — the same content as an editable document.
- **PDF** — proposal and presentation PDFs rendered via headless LibreOffice.
- **Config slide** — a standalone configuration slide (PPTX + PDF).
- **Multi-site exports** — one combined document with per-cluster sections, in any format.
- **Cluster network diagrams** — a per-recommendation C4-style network diagram embedded in the exports.
- **Replication topology** — a cluster-to-cluster replication diagram for multi-site designs.
- **Utilization & benchmark charts** — utilization bars and benchmark visuals in the deck and document.
- **Editable vs. read-only** — editable source files (PPTX / Word) for Scale users; read-only PDF for everyone else.

### Localization
- **15 languages, UI *and* documents** — EN, DE, FR, NL, ES, IT, PT, JA, SV, DA, NO, FI, ET, LV, LT. Every generated export is localized, with CJK-capable fonts bundled for Japanese.

### Accounts, multi-tenancy & collaboration
- **Optional accounts** — sign-up, login, logout, email verification, and password reset.
- **Email-domain tenancy** — users are grouped into tenants by their email domain automatically.
- **Roles** — super-admin, tenant-admin, and user.
- **Saved sizings** — save, reload, update, and delete sizings against your account.
- **Share by code** — hand a colleague a short code to open a shared sizing.

### Administration (live, no redeploy)
- **Hardware catalog** — create / edit / delete models, CPUs, NICs, and drives, plus their per-model compatibility.
- **Drive IOPS & sizing tunables** — edit the per-drive IOPS table and the sizing constants live, with reset-to-defaults.
- **Catalog import/export** — bulk-manage the catalog and models via Excel, with a downloadable template.
- **User & tenant management** — list / disable / restore / delete users, change roles, reset passwords, find stale accounts, purge; assign tenant admins and block tenants.
- **Config oversight** — list and purge any saved sizing.
- **Email/SMTP settings** — configure and test outbound mail.
- **Audit log** — a super-admin audit trail of sensitive actions.

### Security, privacy & compliance
- **Rate limiting** — per-client-IP limits (Redis-backed for exact cross-worker enforcement) on auth, export, and enumeration-prone endpoints.
- **Session & auth hardening** — session rotation on login/signup, full clear on logout, constant-time login (no account enumeration), password-length caps, and a CSRF same-origin guard.
- **Email verification** — mandatory once SMTP is configured, with a time-boxed temporary-suspend grace.
- **Input hardening** — upload content sniffing, HTML-escaped warnings, and an SMTP SSRF guard.
- **GDPR retention** — soft-delete plus hard-delete after a retention window via a daily job, plus on-demand purge.
- **Safe-by-default config** — refuses to boot in production without `SECRET_KEY`; debug mode is env-gated off.

### Performance & resilience
- **Concurrency-guarded exports** — the CPU-heavy document/PDF generation is admission-controlled: a bounded number build at once and excess requests shed gracefully (HTTP 503), so an export burst can't starve interactive traffic.
- **Threaded app tier** — gunicorn gthread workers keep light traffic (page loads, sizing calls) responsive while slow exports run.
- **Stateless app tier** — signed-cookie sessions and Redis-shared rate limits make the app horizontally scalable behind a load balancer.

## Architecture

| Layer | Technology |
|-------|-----------|
| Web framework | Flask 3 + gunicorn (3 workers × 6 threads, gthread) |
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
