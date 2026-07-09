# Ops / Maintenance Container — Plan

Status: **plan only** (no code changed). A design for extracting all single-run
and singleton responsibilities out of the app pods into one dedicated workload,
so the sizer app tier can be scaled horizontally (N replicas behind a load
balancer / K8s Service) safely.

---

## 1. Why

Today [`entrypoint.sh`](../entrypoint.sh) runs `python seed.py` on **every** app
container boot. `seed_all()` does, in order:

1. `db.create_all()` (create missing tables)
2. `_migrate_schema()` — hand-rolled additive column migrations
3. reference-data seed (CPU / NIC / drive catalogs, models, RAM, IOPS defaults)
4. `_bootstrap_super_admin()`
5. `_purge_on_boot()` — retention purge

…and separately, an **in-app daily scheduler thread** runs the retention/GDPR
purge on a timer.

All of this is fine for **one** instance. The moment you run N app replicas
under Kubernetes it breaks:

- N containers run `create_all()` + DDL **concurrently** against the shared DB → races.
- N containers run the purge thread → the GDPR purge runs **N times at once**.
- `create_all()` can't add columns, so schema changes are already fragile.

**Goal:** move every one-time and singleton task into a dedicated ops workload.
App pods then become pure, stateless request servers (gunicorn only).

---

## 2. Design principle: one image, several roles

Build **one image** (reuse the app image, or a slim DB-only variant — see §8) and
select behaviour by **command**, run under three different K8s workload types
matched to three execution models:

| Execution model | Example tasks | K8s workload | When |
| --- | --- | --- | --- |
| Run-once-per-deploy | migrate, seed, backup-before-upgrade | **Job** (Helm pre-upgrade hook) | each deploy, before app rollout |
| Scheduled | purge, account-state transitions, daily backup, integrity check | **CronJob** (one per task) | on a cron |
| Precondition check | "is schema at head?" | ops `check` command | app pod startup (initContainer) |

This is the standard "same image, different entrypoint" pattern (cf. `rake
db:migrate` vs a worker). It guarantees the ops tasks and the app share identical
model definitions.

---

## 3. Task inventory — what moves in

| Task | Current location | New home | Trigger |
| --- | --- | --- | --- |
| Schema migration | `create_all()` + `_migrate_schema()` | Alembic, ops **Job** | per deploy |
| Reference-data seed | `seed.py` `_get_or_create_*` | ops **Job** (idempotent) | per deploy |
| Super-admin bootstrap | `_bootstrap_super_admin()` | ops **Job** | per deploy |
| Retention purge | in-app thread + `_purge_on_boot()` | **CronJob** | daily |
| Time-based account transitions (unverified-account expiry, 30-min temp-suspend auto-resume, lockout expiry) | on-request / scattered | **CronJob** | every ~10 min |
| DB backup | *(none today)* | **CronJob** + pre-upgrade hook | daily + per deploy |
| Data-integrity check | *(none today)* | **CronJob** | daily |
| Schema-current precondition | *(none today)* | ops `check` cmd | app startup gate |

---

## 4. Schema migrations — the "run only what's missing" engine

Adopt **Alembic** (SQLAlchemy-native; `Flask-Migrate` is the optional thin Flask
CLI wrapper).

**How it satisfies "check what's needed before applying a schema change":**
Alembic stores an `alembic_version` marker in the DB. `alembic upgrade head`
computes the gap between the applied revision and the target and runs **only the
missing revisions**, in order. That is exactly "run if not there" — but versioned
and ordered, which the current `create_all()` (can't add columns) plus
hand-rolled `_migrate_schema()` cannot give you robustly.

**Adoption on the existing (already-populated) DB** — one-time:
1. Generate an initial revision describing the current schema.
2. `alembic stamp head` on already-deployed DBs so they're marked at baseline
   **without** re-running DDL. Fresh/empty DBs run the whole chain from zero.

**Robustness rules:**
- **Advisory lock.** Wrap migrate in a Postgres `pg_advisory_lock` so two
  overlapping Jobs (bad redeploy / Job retry) serialize — one migrates, the other
  waits then no-ops.
- **Expand/contract discipline** (for zero-downtime rolling deploys): ship
  additive, backward-compatible migrations first (old *and* new app pods both work
  mid-rollout); do destructive changes a release later, once no running pod needs
  the old column. This is a process rule, not code, but it must be stated.
- Retire `db.create_all()` and `_migrate_schema()` — their intent becomes Alembic
  revisions.

---

## 5. Seeding — idempotent and version-marked

- Keep the existing check-then-insert helpers (`_get_or_create_cpu/nic/drive` are
  already idempotent); relocate them into an `ops/seed` module.
- Add a **seed-version marker** (a row in a small `seed_meta` table, or a
  `SizingSetting` key) so the Job can skip a catalog already at the current version
  instead of re-scanning 625 CPUs every deploy — "catalog v3 loaded → skip."
- **Never clobber user-editable rows.** Honour the existing rule (see
  [seed.py:182](../app/seed.py#L182): *"already back-filled; respect later admin
  edits"*) — reference seed touches only reference data.
- Super-admin bootstrap stays create-if-absent from env.

---

## 6. Scheduler — one CronJob per task (recommended)

**Recommended:** a **K8s CronJob per scheduled task**, not a long-lived scheduler
process. Rationale:

- No always-on process to keep alive, **no leader election**.
- K8s owns the schedule, retry, backoff, and Job history (built-in audit trail).
- Each run is a short-lived, observable pod. `concurrencyPolicy: Forbid` prevents
  overlap.

Move `purge_expired()` out of the in-app thread — the app schedules nothing.

**Alternative** (if you'd rather keep scheduling in code): a single Deployment,
`replicas: 1`, running APScheduler. Simpler code reuse, but you own liveness and
it's a single point of failure for the schedule; needs a lock the moment it's ever
>1 replica. CronJob is the more robust default.

**Dead-man's-switch (important).** The retention purge is a GDPR obligation, so a
*silent* failure is a compliance risk, not just an ops annoyance. Each run should
ping a heartbeat (healthchecks.io / Prometheus Pushgateway / a `last_purge_at`
row) and **alert if no success in >25h**. This is the single highest-value
robustness add for the scheduler.

---

## 7. "Anything else to make it more robust?" — additions

1. **Pre-upgrade database backup.** A `pg_dump` to object storage (S3/GCS) as a
   Helm **pre-upgrade hook**, ordered *before* migrate. Bad migration + fresh
   backup = recoverable. Plus a daily backup CronJob. Highest-value item here.
2. **Least-privilege DB roles.** The ops container needs **DDL** (schema changes);
   the app pods only need **DML**. Split into two Postgres roles so the app runs as
   a user that *cannot* alter schema. Having a dedicated migration container is
   what makes this split practical — and it shrinks the blast radius of any
   app-tier compromise.
3. **Precondition / health gate.** An ops `check` subcommand ("DB reachable +
   schema at head?"). App pods run it as an **initContainer** so they refuse to
   start against an un-migrated DB — prevents serving on a stale schema during a
   botched rollout.
4. **Data-integrity checks.** A daily job that finds orphans the purge/link logic
   could leave (dangling `ScaleConfigLink` rows, users without a tenant, configs
   with no owner) and reports/heals. Cheap insurance given purge deletes across
   several tables.
5. **Structured logging + metrics** from every job: revision applied, rows purged
   per table, backup size/duration, exit status. CronJob Job history gives the
   audit trail for free.
6. **Backup restore drills** (periodic, lower priority): restore the latest dump
   into a throwaway DB and run the integrity check — proves backups are actually
   restorable, not merely written.
7. **Idempotent + re-runnable, always.** K8s retries Jobs; every task must be safe
   to run twice (check-then-act, transactions, advisory locks). Hard invariant.
8. **Failure handling / rollback.** Job `backoffLimit` and `activeDeadlineSeconds`
   (a hung migration must not block forever); prefer **roll-forward** (a new fixing
   migration) over Alembic downgrade (risky); backup is the ultimate fallback.
9. **Batch email, if it ever exists.** Any future batch/notification email (e.g.
   "your data will be purged in 7 days") belongs here, not in app pods. Today email
   is request-driven (verification) so there's nothing yet — flagged for later.

---

## 8. Kubernetes wiring (topology)

```
Helm pre-upgrade hooks (ordered by hook-weight):
    Job: backup   →   Job: migrate (advisory-locked)   →   Job: seed
                                    │  (all must succeed)
                                    ▼
App Deployment (N replicas, HPA on CPU):
    initContainer: ops `check`  (blocks until schema == head)
    container:     gunicorn only   (entrypoint no longer seeds)

CronJobs (concurrencyPolicy: Forbid):
    purge                (daily)
    account-transitions  (~every 10 min)
    backup               (daily)
    integrity-check      (daily)
```

- **Image:** phase 1 reuse the app image (parity, simplest). Phase 2 optional slim
  ops image (no LibreOffice / cairo / fonts) for faster CronJob spin-up; share a
  base or multi-stage build to avoid drift.
- **Secrets:** app-role DB URL, ops-role DB URL, object-store creds, `SECRET_KEY` —
  all K8s Secrets. (`SECRET_KEY` must be identical across all app pods so signed-
  cookie sessions validate on any replica.)

---

## 9. Code / repo changes implied (inventory — no code yet)

- Add `alembic/` + `alembic.ini`; generate a baseline revision from current models;
  add a `stamp`-existing-DB step.
- New `ops/` package with subcommands: `migrate`, `seed`, `check`, `purge`,
  `account-transitions`, `backup`, `integrity` (thin argparse/click CLI over the
  same model imports).
- Refactor: move `seed_all()` internals into `ops/seed`; keep `purge_expired()` but
  call it from the CronJob, not the app thread; delete `db.create_all()` +
  `_migrate_schema()` once Alembic owns schema.
- [`entrypoint.sh`](../entrypoint.sh): drop `python seed.py`; app boots gunicorn
  only.
- `Dockerfile`: add an ops entrypoint (or a second slim stage).

---

## 10. Phased rollout (lowest-risk order)

1. **Introduce Alembic** + baseline-stamp existing DBs. No behaviour change yet;
   `create_all()` still works alongside until cutover.
2. **Extract** seed/purge into `ops/` + CLI; entrypoint still calls it (still single
   instance today) — pure refactor, testable on testenv.
3. **Split** into K8s Job + CronJobs; remove seed from the app entrypoint; add the
   initContainer `check`.
4. **Add** pre-upgrade backup + dead-man's-switch alerting.
5. **Split** DB roles (app = DML-only).

Each phase is independently shippable and verifiable on testenv before the next.

---

## 11. Decisions (provisional — revisit when building)

- **Migration tool: Alembic** (raw, not the Flask-Migrate wrapper).
- **Scheduler: one CronJob per task** (no long-lived APScheduler daemon; K8s owns
  schedule/retry/history).
- **Ops image: reuse the app image first**; a slim ops image is a later
  optimization only if CronJob spin-up time bites.
- **Platform: self-hosted on Scale Computing.** → Backups to a self-hosted
  S3-compatible store (MinIO) or an NFS/SMB volume on Scale Computing storage.
  Dead-man's-switch via a self-hosted monitor (Uptime Kuma / self-hosted
  healthchecks.io / Prometheus Alertmanager) — no external SaaS dependency for the
  compliance-critical purge alert.

Still open (sub-details, not architecture): backup **retention window** (days) and
which self-hosted **alerting tool**.
