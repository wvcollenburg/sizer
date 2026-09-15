"""Vendor chassis for Validated (software-only) sizing.

A Validated recommendation is still sized against the sizer's own Model
catalog (HC1450, HC3450F, ...), but what the customer buys is a vendor server:
the same SC model is sold on Lenovo, Dell, HPE and Supermicro hardware. The
HCL catalog (hcl_models.HclPlatform) is the authority on which vendor server
carries which SC model, so Validated sizing picks ONE vendor and:

* only recommends models the HCL lists for that vendor (a model without an
  active platform for the vendor is not a buildable Validated box), and
* speaks in vendor chassis only ("Lenovo ThinkSystem SR630V3"). The SC model
  split into all-flash / hybrid / GPU variants is Scale's product line, not the
  vendor's: HC1450, HC3450F and HC3450FG are all one SR630V3 in different
  configurations. So a chassis is the unit a Validated sizing lists, targets
  and de-duplicates on. The SC model is named in one place only — quietly, as
  "HCxxxx equivalent" in the result card's footer — never as the product name
  and never in an export.

Validated-only models (admin-defined boxes with no SC model and no HCL entry,
e.g. a repurposed Dell VxRail) carry their own ``Model.vendor`` instead. They
join that vendor's chassis set under their own chassis text, and a vendor that
exists only through such models still appears in the Vendor list.

The recommendation keeps ``model`` = the SC model internally: it is the catalog
identity that refs, the fingerprint and the BOM fit key on. The chassis travels
alongside as ``vendor_chassis`` / ``vendor_chassis_key`` and every display site
reads it through :func:`rec_display_model`.
"""
from collections import Counter

# Display names for the lower-case brand keys the HCL scrape stores. A brand
# the site adds later falls back to title case, which is right for most names.
BRAND_LABELS = {
    "dell": "Dell",
    "hpe": "HPE",
    "lenovo": "Lenovo",
    "supermicro": "Supermicro",
}

# The vendor a fresh sizing starts on. The current Active catalog is mostly
# Lenovo-built, so it is the least surprising default; any listed vendor wins
# when Lenovo is absent from the catalog.
DEFAULT_VENDOR = "lenovo"


def brand_label(brand):
    brand = (brand or "").strip()
    return BRAND_LABELS.get(brand.lower(), brand.title())


def sc_model_key(name):
    """Normalise an SC model name for matching the sizer catalog to the HCL.

    Both sides append variant suffixes after a dash that do not change the
    box: the sizer splits HE155 into HE155-1/HE155-2 configurations, the HCL
    lists Dell's HC5250D as HC5250D-V. Letters glued to the number (the D in
    HC1650D, the F in HC3450F) are real model differences and are kept.
    """
    return (name or "").strip().upper().split("-", 1)[0]


def chassis_key(platform):
    """Identity of a vendor chassis: brand + normalised SERVER line, so
    'ThinkSystem SR630V3' and 'ThinkSystem SR630 V3' are the same box."""
    from hcl_models import server_key
    return "%s/%s" % (platform.brand, server_key(platform.server) or sc_model_key(platform.sc_model))


def chassis_label(platform):
    """'Lenovo ThinkSystem SR630V3' for an HclPlatform."""
    server = (platform.server or "").strip() or platform.sc_model
    return "%s %s" % (brand_label(platform.brand), server)


def validated_only_label(model):
    """A validated-only model is named after its own chassis text, prefixed
    with the vendor unless the text already starts with it ('Dell-VxRAIL'
    stays as is, 'VxRail P670F' becomes 'Dell VxRail P670F')."""
    brand = brand_label(model.vendor)
    text = (model.chassis or "").strip() or model.name
    if text.lower().startswith(brand.lower()):
        return text
    return "%s %s" % (brand, text)


def validated_only_key(model):
    from hcl_models import server_key
    return "%s/vo-%s" % (model.vendor, server_key(model.chassis) or model.name.lower())


def list_vendors():
    """Vendors with at least one active HCL platform, default first.

    Returns [] when the HCL catalog is empty (no scrape approved yet), and
    never raises: the sizing page renders this and must load regardless.
    """
    try:
        from database import db
        from hcl_models import HclPlatform, STATUS_ACTIVE
        from orm_models import Model
        brands = [b for (b,) in db.session.query(HclPlatform.brand)
                  .filter(HclPlatform.status == STATUS_ACTIVE)
                  .distinct().all() if b]
        brands += [b for (b,) in db.session.query(Model.vendor)
                   .filter(Model.validated_only == True,   # noqa: E712
                           Model.vendor.isnot(None))
                   .distinct().all() if b]
    except Exception:                                   # noqa: BLE001
        return []
    brands = sorted(set(b.lower() for b in brands),
                    key=lambda b: (b != DEFAULT_VENDOR, brand_label(b).lower()))
    return [{"brand": b, "label": brand_label(b)} for b in brands]


def resolve_vendor(vendor, vendors=None):
    """The vendor a Validated sizing actually uses: the requested one when the
    HCL lists it, otherwise the default. None only when no vendor is listed."""
    vendors = list_vendors() if vendors is None else vendors
    brands = [v["brand"] for v in vendors]
    vendor = (vendor or "").strip().lower()
    if vendor in brands:
        return vendor
    return brands[0] if brands else None


class VendorChassis:
    """One vendor's buildable chassis, resolved from the HCL once per request.

    ``for_model(name)`` answers "which chassis is this SC model on?" (None when
    the vendor does not build it). Every platform on the same chassis gets the
    same label — the most common spelling of the SERVER line — so two cards
    for one box can never read differently.
    """

    def __init__(self, vendor):
        self.vendor = vendor
        self._by_model = {}
        # Validated-only models, by exact model name. The HCL name match never
        # applies to them: one for another vendor (or with no vendor set) is
        # simply not buildable here, even if its name looks like an SC model.
        self._validated_only = {}
        self._other_validated_only = set()
        if not vendor:
            return
        from orm_models import Model
        for m in Model.query.filter(Model.validated_only == True).all():   # noqa: E712
            if (m.vendor or "") == vendor.lower():
                self._validated_only[m.name] = {
                    "key": validated_only_key(m), "label": validated_only_label(m),
                    "platform": None}
            else:
                self._other_validated_only.add(m.name)
        from hcl_models import HclPlatform, STATUS_ACTIVE
        rows = (HclPlatform.query
                .filter(HclPlatform.brand == vendor.lower(),
                        HclPlatform.status == STATUS_ACTIVE)
                .order_by(HclPlatform.sc_model).all())
        spellings = {}
        for p in rows:
            spellings.setdefault(chassis_key(p), Counter())[chassis_label(p)] += 1
        labels = {k: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                  for k, c in spellings.items()}
        for p in rows:
            model_key = sc_model_key(p.sc_model)
            # An exact SC model name beats a suffix-normalised one when both
            # exist (HC5250D and HC5250D-V on one brand).
            if model_key in self._by_model and p.sc_model.upper() != model_key:
                continue
            key = chassis_key(p)
            self._by_model[model_key] = {"key": key, "label": labels[key], "platform": p}

    def __bool__(self):
        return bool(self._by_model or self._validated_only)

    def for_model(self, model_name):
        if model_name in self._validated_only:
            return self._validated_only[model_name]
        if model_name in self._other_validated_only:
            return None
        return self._by_model.get(sc_model_key(model_name))

    def resolve_target(self, target):
        """A "size for" target in Validated mode names a chassis key. A bare
        SC model (a sizing saved before chassis targets) maps to its chassis.
        Returns (chassis_key or None, label or None)."""
        target = (target or "").strip()
        if not target:
            return None, None
        for entry in list(self._by_model.values()) + list(self._validated_only.values()):
            if entry["key"] == target:
                return entry["key"], entry["label"]
        entry = self.for_model(target)
        if entry:
            return entry["key"], entry["label"]
        return target, target


def rec_display_model(rec):
    """What a recommendation is called on screen and in exports."""
    if not isinstance(rec, dict):
        return ""
    return rec.get("vendor_chassis") or rec.get("model") or ""
