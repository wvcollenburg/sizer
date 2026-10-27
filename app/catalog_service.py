"""The node catalog as the sizing pickers and the engine see it.

``appliance_models`` is what ``GET /api/models`` (appliance mode) returned
inline; it moved here so the MCP ``list_node_models`` tool lists exactly the
models the engine will consider, from one query (docs/mcp/01-tool-design.md
§3.2). Not fingerprinted: it reads the catalog and changes no number.
"""
from sqlalchemy.orm import joinedload

import hcl_vendor
from orm_models import (
    Model, StorageConfig, ModelCpuOption, ModelNicOption, StorageConfigDrive,
)


def appliance_models(status_filter="active", validated=False, for_sizing=False,
                     vendor=None):
    """``{name: Model.to_dict()}`` (certified), or ``{chassis_key: {category,
    status, vendor_chassis, validated_only}}`` (validated).

    The "Size For Model" picker must list exactly the models the recommendation
    engine will consider for the chosen sizing mode (see
    recommend.generate_recommendations): validated mode includes
    validated-only platforms but drops NVMe+SSD (1+1) models, while certified
    mode is the inverse. ``for_sizing`` drops admin-excluded models, as the
    engine does; the Appliance calculator (no sizing) still lists them, since
    building one by hand is not a recommendation."""
    query = Model.query.options(
        joinedload(Model.cpu_links).joinedload(ModelCpuOption.cpu),
        joinedload(Model.nic_links).joinedload(ModelNicOption.nic),
        joinedload(Model.ram_options),
        joinedload(Model.storage_config)
            .joinedload(StorageConfig.drive_links)
            .joinedload(StorageConfigDrive.drive),
    )
    if status_filter == "active":
        query = query.filter(Model.status == "Active")
    elif status_filter == "all_current":
        query = query.filter(Model.status.in_(["Active", "EOL"]))
    if not validated:
        # Validated-only platforms have no certified equivalent, so they
        # don't belong in the certified appliance picker.
        query = query.filter(Model.validated_only == False)  # noqa: E712
    if for_sizing:
        query = query.filter(Model.exclude_from_recommendations == False)  # noqa: E712

    # Validated mode sizes against one vendor's HCL platforms and speaks in
    # chassis only: the picker lists each chassis that vendor builds ONCE,
    # keyed by chassis — never the SC models behind it.
    vendor_chassis = None
    if validated:
        vendor_chassis = hcl_vendor.VendorChassis(hcl_vendor.resolve_vendor(vendor))

    models = {}
    for m in query.order_by(Model.category, Model.name).all():
        # Validated mode can't use NVMe+SSD models (inherently 2-disk), so
        # exclude them to match what the engine will actually size.
        if validated and m.storage_config \
                and m.storage_config.storage_type == "nvme_and_ssd":
            continue
        if validated:
            chassis = vendor_chassis.for_model(m.name)
            if chassis is None:
                continue
            entry = models.setdefault(chassis["key"], {
                "category": m.form_factor or "",
                "status": m.status,
                "vendor_chassis": chassis["label"],
                # An admin-defined box, not an HCL-listed SC model.
                "validated_only": chassis["platform"] is None,
            })
            # A chassis is current while any configuration on it is.
            if m.status == "Active":
                entry["status"] = "Active"
            continue
        models[m.name] = m.to_dict()
    return models
