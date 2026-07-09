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

## 5. Seeding — the reference catalog (idempotent, version-marked)

**What "seed" means here — and what it is NOT.** The seed populates the *product
reference catalog*: the Scale Computing models, the CPU / NIC / drive catalogs,
model↔component compatibility, RAM options, per-drive IOPS defaults, sizing
tunables, and the initial super-admin. This is the data that makes the sizer able
to size anything — the product's "brain," identical across every deployment and
versioned with the code.

It is **not** the same data as the LiveOptics / RVTools Excel import, and the two
are **not substitutes**:

| | Reference catalog (seed) | Excel import (LiveOptics/RVTools) |
| --- | --- | --- |
| What it is | Scale hardware you size *toward* | The customer's environment you size *from* |
| Source | The product (same everywhere) | Each customer, per sizing |
| Lifetime | Persistent product data | Transient input for one run |
| Home | Seeded once, then edited via admin UI | Uploaded per request, never persisted as catalog |

The import populates the *thing being sized* (customer VMs/hosts); it can never
supply the *catalog you size against*. So the catalog seed **cannot be dropped "in
favour of the import"** — an un-seeded DB is a functionally dead tool. What the
seed *should* change is **when/where** it runs (a run-once ops Job, not every app
boot) and **how** the data is expressed (see the evolution note below).

**Approach:**

- Relocate the existing check-then-insert helpers (`_get_or_create_cpu/nic/drive`
  are already idempotent) into an `ops/seed` module; run them from the ops Job, not
  the app entrypoint.
- Add a **seed-version marker** (a row in a small `seed_meta` table, or a
  `SizingSetting` key) so the Job skips a catalog already at the current version
  instead of re-scanning 625 CPUs every deploy — "catalog v3 loaded → skip."
- **Never clobber user-editable rows.** Honour the existing rule (see
  [seed.py:182](../app/seed.py#L182): *"already back-filled; respect later admin
  edits"*) — reference seed touches only reference data the admin hasn't overridden.
- Super-admin bootstrap stays create-if-absent from env.

**Recommended evolution — catalog as a versioned data file.** Today the catalog
lives as imperative Python (`_get_or_create_*` calls). Better: ship it as a
*declarative, versioned data artifact* (JSON/CSV bundled in the image — an Excel
the ops Job imports is fine too) that the seed loads idempotently. Benefits:

- The catalog becomes data — reviewable in diffs and **versioned with the release**,
  so a new model or updated benchmark ships in a normal deploy, not a code edit.
- One loader handles fresh installs *and* DB-restore recovery, reproducibly.
- Pairs naturally with the existing admin **export/import catalog** capability as a
  maintenance + backup path.

**Guard-rail:** whatever the format, the catalog must stay **bundled with the
product and loaded automatically** by the ops Job. Do *not* make a manual upload
the only path to a working catalog — that yields a dead first run, no disaster
recovery, and deployment drift.

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

---

## 12. Database resilience (backups · PITR · HA · DR)

The app tier is stateless and horizontally scaled, so **the database is the last
single stateful point** in the system. Redundancy here is layered — each layer
protects against a *different* failure, and on Scale Computing several are native.

**Replication ≠ backup — they cover opposite failures.** Replication (VM- or
DB-level) copies every write to a second copy → protects against *losing a copy*
(host/cluster dies). It does **not** protect against a bad migration, logical
corruption, or an accidental `DELETE` — those replicate to the standby instantly
and now both copies are wrong. Only **backups** save you from that class. So:
backups always; replication is an availability add-on, never a backup substitute.

| Layer | Protects against | On Scale Computing | Effort |
| --- | --- | --- | --- |
| Base backups (pg_dump/basebackup) | logical corruption, bad migration, accidental delete | CronJob → MinIO/NFS (§7) | low |
| WAL archiving / log-shipping → **PITR** | above, but RPO in *minutes* not 24h | ship WAL to same store | low–moderate |
| **Scale native VM HA** | a physical node failing | built in — VM restarts on a healthy node | ~free |
| **Scale cross-cluster VM replication** | whole cluster/site loss (DR) | native async VM replication | moderate (platform) |
| Postgres streaming replication (hot standby) | near-zero RPO + fast failover | self-run standby | high |

**Recommendation for this workload:**

1. **Backups** — the floor (already planned).
2. **WAL archiving (log-shipping) → PITR** — the sweet spot: RPO drops from "last
   nightly dump" to "last archived segment" (minutes) with **no live standby to
   babysit**.
3. **Availability → Scale native VM HA** (free; the DB VM survives a host failure).
4. **DR → Scale cross-cluster VM replication.** Caveat: block/VM replication of a
   *running* Postgres is **crash-consistent** — Postgres recovers on boot via WAL
   (fine), it's just not a transaction-clean snapshot.
5. **Postgres streaming standby → defer** until a concrete RPO/RTO requirement
   forces it; highest complexity, least justified for a sizing tool.

**Resilience you don't monitor is theatre.** A few-minutes RTO is worthless if no
one who can act notices — so every layer needs a *verified* signal, not just a
config:

- Backups: alert if the last **successful** backup is older than its interval (the
  classic failure is "we had backups… from three weeks ago").
- WAL archiving: alert on archiver lag/failure — a stalled archiver silently breaks
  PITR.
- Replication (if used): alert on replication lag / broken stream.
- **Restore drills** (§7.6): a backup you've never restored is a hope, not a
  backup. Periodically restore into a throwaway DB and run the integrity check.

---

## 13. Operating with no dedicated operator — self-healing first

**Constraint (drives this whole section):** there is **no dedicated ops/admin
resource** for this tool, and the owner (an EMEA pre-sales Solution Architect)
**travels heavily** — human availability to respond is *unpredictable*, and the
SLA is shared across a small group. This should shape the architecture more than a
raw uptime target does.

**Design principle: optimize for autonomous recovery, not fast human response.**
MTTR must not depend on someone being awake, on the ground, and expert. The goal
isn't five-nines — it's *"never down for hours because the one person who can fix
it is on a flight."* Concretely: **self-heal what you can; degrade gracefully and
stay up for the rest; and make monitoring escalate to a group, not a person.**

**Application HA — what the orchestration direction actually buys you.** Beyond
scaling, orchestration removes the human from the app-tier recovery loop:

- **≥2 replicas behind the Service/LB** → a pod or node dying is invisible;
  survivors serve, the orchestrator reschedules the dead one. No human.
- **liveness probes** → a hung pod is auto-restarted.
- **readiness probes** → an unhealthy pod is auto-pulled from rotation.
- **anti-affinity across Scale nodes (ideally clusters)** → one node/cluster loss
  can't take every replica.
- **rolling deploy + auto-rollback** → a bad release halts itself instead of taking
  the app down.

That is app HA *as self-healing* — exactly what an unpredictable-operator situation
needs.

**But weigh the orchestration burden honestly** — self-managed HA Kubernetes is
itself something to administer, the opposite of what "no dedicated ops" wants. Pick
the lightest option that delivers self-healing:

| Option | App HA / self-healing | Admin burden |
| --- | --- | --- |
| Single VM, docker-compose (today) | container auto-restart (`restart: unless-stopped`) + Scale VM HA — but brief downtime on restart, no zero-downtime deploys | lowest |
| Lightweight K8s (e.g. k3s), 2–3 nodes across Scale hosts | full: multi-replica, self-healing, zero-downtime rolling deploys | moderate (you run k3s) |
| Scale-provided / managed K8s | full, control-plane offloaded | low–moderate, platform-dependent |

**Honest read:** you already get *meaningful* passive self-healing today — Docker's
restart policy plus Scale VM HA bring the app back after a crash or host failure
**without a human**. What single-VM lacks is **zero-downtime** (a few minutes' blip
during a restart) and **surviving a *wedged* VM** (vs a cleanly crashed one). If a
few minutes of occasional downtime is acceptable for a sizing tool — likely — then
the jump to self-managed K8s should be justified by the **scaling** need, not bought
purely for HA. If you do want true app HA with minimal burden, a small **k3s across
≥2 Scale nodes** is the pragmatic middle.

**Monitoring, reframed for a shared / unpredictable on-call.** Its job is *not*
"page the owner." It is:

- **Trigger automation first.** Where a condition has an automated fix (restart,
  failover, scale-out), let the system act; the alert is FYI, not a call to arms.
- **Escalate to a group with a rotation** — a shared channel + an ack-or-escalate
  policy (unacked in N min → next person), so a plane-bound owner isn't the single
  point of *human* failure. This is the human-layer equivalent of multi-replica.
- **Dead-man's-switches over threshold alerts** for silently-failing paths (purge,
  backups, WAL archiving): alert on the *absence of success*.
- **A runbook** terse enough that a non-expert in the group can execute the top ~5
  recoveries (restart X, restore latest backup, roll back a deploy, fail over the
  DB, drain a bad node) without deep knowledge.
- **Buy time by degrading, not dying.** The export gate (503-sheds under overload,
  tool stays up) is the model — prefer that pattern everywhere. A degraded-but-up
  tool survives an unattended incident; a hard-down one doesn't.

**Open decisions this raises:**

- Orchestration target: stay single-VM (+ restart/HA) vs light k3s vs
  Scale-provided K8s — and is that call driven by *scale* or by *HA*?
- Who is in the shared on-call group, and what's the escalation policy / channel?
- Acceptable downtime for the tool (this sets how hard app HA must work).
