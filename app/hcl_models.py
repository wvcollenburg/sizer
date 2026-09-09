"""HCL (hardware compatibility list) catalog ORM — docs/bom-checker-build.md §2.

Why a separate catalog from orm_models' ``validated_*`` tables: those describe
the sizer's own validated node families for *sizing*; this is a mirror of what
hcl.scalecomputing.com publishes as SC//HyperCore-ready platforms and the parts
validated for each, used to *check third-party BOMs*. The two answer different
questions and change on different schedules.

Lifecycle rules that shape the schema:

* Nothing here is written by the scraper directly. A scrape produces
  ``HclPendingChange`` rows; a super admin approves them, and only approval
  mutates platforms/components/devices. The site has gone stale for months
  before, so an unattended diff must never land in the live catalog.
* Rows are never hard-deleted. A part that disappears from the site becomes
  ``status='delisted'`` with a date, so an old BOM check still resolves its
  findings and a re-check can say "delisted on <date>".
* ``first_seen``/``last_seen`` are bookkeeping, not catalog content: the sync
  bumps ``last_seen`` for every entity present in a complete scrape without
  going through the queue.
"""
from database import db
from auth_models import JSON_TYPE, _iso, _utcnow

STATUS_ACTIVE = "active"
STATUS_DELISTED = "delisted"

# Where a component (or platform link) came from. "scrape" is the normal
# case: the entity was seen on hcl.scalecomputing.com. "preview" marks a part
# the HCL team has verified but not yet published, accepted by a super admin
# from a BOM check (bom/preview.py); its absence from the site is expected,
# so the scrape diff never queues its delist, and the first complete scrape
# that DOES list it silently flips it to "scrape".
ORIGIN_SCRAPE = "scrape"
ORIGIN_PREVIEW = "preview"

COMPONENT_KINDS = ("cpu", "nic", "hba", "hdd", "ssd", "gpu")

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"

CHANGE_ADD = "add"
CHANGE_UPDATE = "update"
CHANGE_DELIST = "delist"
CHANGE_RELIST = "relist"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"


class HclPlatform(db.Model):
    """One SC//HyperCore-ready platform card: an SC model number as sold on a
    given vendor's server (the same SC model, e.g. HC1450, exists on Lenovo,
    HPE and Supermicro hardware, so the key is brand + model)."""
    __tablename__ = "hcl_platforms"
    __table_args__ = (
        db.UniqueConstraint("brand", "sc_model", name="uq_hcl_platform_brand_model"),
    )

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(20), nullable=False, index=True)    # lower-case
    sc_model = db.Column(db.String(40), nullable=False, index=True)
    server = db.Column(db.String(200))            # 'ThinkSystem SR630V2'
    form_factor = db.Column(db.String(8))         # 1U / 2U / DT
    socket = db.Column(db.String(20))
    sockets = db.Column(db.Integer)
    max_cores = db.Column(db.Integer)
    memory_type = db.Column(db.String(30))        # 'DDR5 RDIMM'
    ram_slots = db.Column(db.Integer)
    max_ram_gb = db.Column(db.Integer)
    power_supply = db.Column(db.Text)
    cooling = db.Column(db.Text)
    tpm = db.Column(db.Text)
    risers = db.Column(db.Text)
    oob_license = db.Column(db.Text)
    hdd_max = db.Column(db.Integer)
    ssd_max = db.Column(db.Integer)
    nic_listed = db.Column(db.Boolean, nullable=False, default=True)
    # See HclComponent.origin: a "preview" platform was created by a super
    # admin during a pre-publication accept (bom/preview.py) because the HCL
    # does not list the server yet. Deliberately NOT in TRACKED — provenance,
    # not page content — so the scrape diff never queues an "origin changed"
    # row; the first complete scrape that lists (brand, sc_model) flips it to
    # "scrape" silently (hcl_sync.touch_seen).
    origin = db.Column(db.String(10), nullable=False, default=ORIGIN_SCRAPE)
    status = db.Column(db.String(12), nullable=False, default=STATUS_ACTIVE, index=True)
    first_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    delisted_at = db.Column(db.DateTime(timezone=True))

    links = db.relationship("HclPlatformComponent", back_populates="platform",
                            cascade="all, delete-orphan")

    # Fields the scrape diff tracks (everything a card/detail page states).
    TRACKED = ("server", "form_factor", "socket", "sockets", "max_cores",
               "memory_type", "ram_slots", "max_ram_gb", "power_supply",
               "cooling", "tpm", "risers", "oob_license", "hdd_max", "ssd_max",
               "nic_listed")

    @property
    def key(self):
        return "%s/%s" % (self.brand, self.sc_model)

    def to_dict(self, with_components=False):
        d = {
            "id": self.id,
            "key": self.key,
            "brand": self.brand,
            "sc_model": self.sc_model,
            "server": self.server,
            "server_key": server_key(self.server),
            "form_factor": self.form_factor,
            "socket": self.socket,
            "sockets": self.sockets,
            "max_cores": self.max_cores,
            "memory_type": self.memory_type,
            "ram_slots": self.ram_slots,
            "max_ram_gb": self.max_ram_gb,
            "power_supply": self.power_supply,
            "cooling": self.cooling,
            "tpm": self.tpm,
            "risers": self.risers,
            "oob_license": self.oob_license,
            "hdd_max": self.hdd_max,
            "ssd_max": self.ssd_max,
            "nic_listed": self.nic_listed,
            "origin": self.origin,
            "status": self.status,
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "delisted_at": _iso(self.delisted_at),
            "component_count": sum(1 for l in self.links if l.status == STATUS_ACTIVE),
        }
        if with_components:
            d["components"] = [
                dict(l.component.to_dict(), tce=l.tce, link_status=l.status,
                     link_origin=l.origin)
                for l in sorted(self.links, key=lambda l: (l.component.kind, l.component.part_number))
            ]
        return d


class HclComponent(db.Model):
    """A vendor part validated for at least one platform. Keyed on
    (kind, part_number): the same Intel CPU part appears under many platforms
    and brands, so the brand lives on the platform link, not here."""
    __tablename__ = "hcl_components"
    __table_args__ = (
        db.UniqueConstraint("kind", "part_number", name="uq_hcl_component_kind_part"),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(8), nullable=False, index=True)
    part_number = db.Column(db.String(80), nullable=False, index=True)
    description = db.Column(db.String(300), nullable=False)
    attrs = db.Column(JSON_TYPE)          # hcl_scrape.component_attrs() output
    tce = db.Column(db.Boolean, nullable=False, default=False)   # TCE on any platform
    # Deliberately NOT in TRACKED: origin is provenance, not page content —
    # the scrape diff must never produce an "origin changed" queue row.
    origin = db.Column(db.String(10), nullable=False, default=ORIGIN_SCRAPE)
    status = db.Column(db.String(12), nullable=False, default=STATUS_ACTIVE, index=True)
    first_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    delisted_at = db.Column(db.DateTime(timezone=True))

    links = db.relationship("HclPlatformComponent", back_populates="component",
                            cascade="all, delete-orphan")

    TRACKED = ("description", "tce", "attrs")

    @property
    def key(self):
        return "%s/%s" % (self.kind, self.part_number)

    def to_dict(self, with_platforms=False):
        d = {
            "id": self.id,
            "key": self.key,
            "kind": self.kind,
            "part_number": self.part_number,
            "description": self.description,
            "attrs": self.attrs or {},
            "tce": self.tce,
            "origin": self.origin,
            "status": self.status,
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "delisted_at": _iso(self.delisted_at),
        }
        if with_platforms:
            d["platforms"] = [
                {"key": l.platform.key, "brand": l.platform.brand,
                 "sc_model": l.platform.sc_model, "tce": l.tce,
                 "link_status": l.status, "link_origin": l.origin}
                for l in sorted(self.links, key=lambda l: (l.platform.brand, l.platform.sc_model))
            ]
        return d


class HclPlatformComponent(db.Model):
    """Which parts a platform's detail page lists. TCE is per platform on the
    site (the badge sits on the option inside one platform's select)."""
    __tablename__ = "hcl_platform_components"
    __table_args__ = (
        db.UniqueConstraint("platform_id", "component_id", name="uq_hcl_platform_component"),
    )

    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.Integer, db.ForeignKey("hcl_platforms.id"),
                            nullable=False, index=True)
    component_id = db.Column(db.Integer, db.ForeignKey("hcl_components.id"),
                             nullable=False, index=True)
    tce = db.Column(db.Boolean, nullable=False, default=False)
    # See HclComponent.origin: a "preview" link was accepted ahead of
    # publication, so the scrape's whole-list sync must not delist it.
    origin = db.Column(db.String(10), nullable=False, default=ORIGIN_SCRAPE)
    status = db.Column(db.String(12), nullable=False, default=STATUS_ACTIVE)
    first_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    platform = db.relationship("HclPlatform", back_populates="links")
    component = db.relationship("HclComponent", back_populates="links")


class HclDevice(db.Model):
    """PCI device table from /hcl/devinfo — vendor/device ids are the one
    robust key a BOM could ever be matched on when part numbers differ."""
    __tablename__ = "hcl_devices"
    __table_args__ = (
        db.UniqueConstraint("ven_id", "dev_id", name="uq_hcl_device_ids"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ven_id = db.Column(db.String(8), nullable=False, index=True)   # lower hex
    dev_id = db.Column(db.String(8), nullable=False, index=True)
    description = db.Column(db.String(200))
    driver = db.Column(db.String(40))
    dev_type = db.Column(db.String(16))       # network / storage / gpu / wifi / fabric
    supported = db.Column(db.Boolean, nullable=False, default=True)
    since = db.Column(db.String(20))
    status = db.Column(db.String(12), nullable=False, default=STATUS_ACTIVE, index=True)
    first_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    delisted_at = db.Column(db.DateTime(timezone=True))

    TRACKED = ("description", "driver", "dev_type", "supported", "since")

    @property
    def key(self):
        return "%s/%s" % (self.ven_id, self.dev_id)

    def to_dict(self):
        return {
            "id": self.id,
            "key": self.key,
            "ven_id": self.ven_id,
            "dev_id": self.dev_id,
            "description": self.description,
            "driver": self.driver,
            "dev_type": self.dev_type,
            "supported": self.supported,
            "since": self.since,
            "status": self.status,
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "delisted_at": _iso(self.delisted_at),
        }


class HclScrapeRun(db.Model):
    """One admin-triggered scrape (or snapshot import). Doubles as the lock:
    a run still 'running' blocks a second one until it finishes or times out."""
    __tablename__ = "hcl_scrape_runs"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(12), nullable=False, default=RUN_QUEUED, index=True)
    source = db.Column(db.String(24), nullable=False, default="scrape")   # scrape | import
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    finished_at = db.Column(db.DateTime(timezone=True))
    pages_total = db.Column(db.Integer, nullable=False, default=0)
    pages_done = db.Column(db.Integer, nullable=False, default=0)
    complete = db.Column(db.Boolean, nullable=False, default=False)
    errors = db.Column(JSON_TYPE)
    summary = db.Column(JSON_TYPE)
    error = db.Column(db.Text)
    triggered_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    changes = db.relationship("HclPendingChange", back_populates="run")

    @property
    def progress(self):
        if self.status in (RUN_SUCCEEDED, RUN_FAILED):
            return 100
        if not self.pages_total:
            return 0
        return int(min(99, 100 * self.pages_done / float(self.pages_total)))

    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "progress": self.progress,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "pages_total": self.pages_total,
            "pages_done": self.pages_done,
            "complete": self.complete,
            "errors": self.errors or [],
            "summary": self.summary or {},
            "error": self.error,
            "triggered_by_user_id": self.triggered_by_user_id,
        }


class HclPendingChange(db.Model):
    """One reviewable difference between a scrape and the live catalog."""
    __tablename__ = "hcl_pending_changes"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("hcl_scrape_runs.id"),
                       nullable=False, index=True)
    entity_type = db.Column(db.String(12), nullable=False, index=True)   # platform|component|device
    entity_key = db.Column(db.String(160), nullable=False, index=True)
    change_kind = db.Column(db.String(8), nullable=False, index=True)    # add|update|delist|relist
    field = db.Column(db.String(40))
    old_value = db.Column(JSON_TYPE)
    new_value = db.Column(JSON_TYPE)
    payload = db.Column(JSON_TYPE)          # full entity record for add/relist
    label = db.Column(db.String(200))
    status = db.Column(db.String(12), nullable=False, default=PENDING, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    decided_at = db.Column(db.DateTime(timezone=True))
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.Text)

    run = db.relationship("HclScrapeRun", back_populates="changes")

    def to_dict(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "entity_type": self.entity_type,
            "entity_key": self.entity_key,
            "change_kind": self.change_kind,
            "field": self.field,
            "old": self.old_value,
            "new": self.new_value,
            "label": self.label,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "decided_at": _iso(self.decided_at),
            "decided_by_user_id": self.decided_by_user_id,
            "note": self.note,
        }


def server_key(server):
    """Normalise a server model string for matching a BOM's serverModel
    against a platform's SERVER line: 'ThinkSystem SR630 V3' and
    'ThinkSystem SR630V3' and 'sr630 v3' all become 'sr630v3'. Vendor family
    words carry no information (the brand is matched separately) and vendors
    are inconsistent about spaces and dashes, so both are dropped."""
    if not server:
        return ""
    s = server.lower()
    for word in ("thinksystem", "thinkcentre", "thinkedge", "poweredge",
                 "proliant", "supermicro", "lenovo", "dell", "hpe", "server"):
        s = s.replace(word, " ")
    return "".join(ch for ch in s if ch.isalnum())
