#!/usr/bin/env python3
"""Build the anonymised demo Live Optics export the manual screenshots use.

    .venv/bin/python tools/manual_demo_data.py SOURCE.xlsx OUT.xlsx

Real exports name customers' hosts, VMs, datastores and IP addresses, and the
manual is read by every partner — so the screenshots must never show one. This
keeps the NUMBERS of a real export (so the sizing looks like a real sizing) and
replaces every piece of text that is not on an allow-list of harmless columns
(OS, power state, hardware model, …) with a made-up value. Allow-list, not
block-list: a column Live Optics adds later is scrubbed by default.

vCLS and vCenter VMs keep a recognisable name so the manual can show the
"Remove VMware artifacts" button doing its job.
"""
import re
import sys
from datetime import datetime

import openpyxl

# Text columns that describe hardware or software, never the customer.
KEEP = {
    "OS", "Manufacturer", "Model", "CPU Description", "Config Status",
    "IsRunning", "Power State", "VM OS", "VMware Tools Version",
    "Connection State", "Template", "VM IO Classification", "Device Type",
    "Vendor", "Lun Type", "SCSI Level", "vStorageSupport", "Software Title",
    "Disk Model", "Disk Vendor", "SSD", "PNIC Vendor", "PNIC Device Name",
    " Image Backup",
}
# Identity columns mapped consistently across sheets, so joins still work.
KIND = {
    "Host Name": "host", "Host": "host", "Server Name": "host", "ESX Host": "host",
    "VM Name": "vm", "Guest Hostname": "vm",
    "Cluster": "cluster", "Datacenter": "dc", "vCenter": "vcenter",
    "Datastore": "ds", "Device Name": "ds",
}
DROP = re.compile(r"^(IP\d|Guest IP\d|Serial No|UUID|InstanceUUID|MOB ID|PNIC Name|Disk Name)$")

CLUSTERS = ["Production", "Remote-Office", "Test-Dev"]
VM_ROLES = ["app", "web", "sql", "file", "dc", "print", "erp", "mail", "rds",
            "backup", "mon", "crm", "log", "proxy", "build", "git", "wiki", "ftp"]


class Mapper:
    def __init__(self):
        self.maps = {k: {} for k in ("host", "vm", "cluster", "dc", "vcenter", "ds")}
        self.n = {k: 0 for k in self.maps}

    def __call__(self, kind, value):
        m = self.maps[kind]
        if value in m:
            return m[value]
        i = self.n[kind]
        self.n[kind] += 1
        if kind == "vm":
            low = str(value).lower()
            if low.startswith("vcls"):
                new = "vCLS-%04x" % (0xa11 + i)
            elif "vcenter" in low or "vcsa" in low:
                new = "vcenter01"
            else:
                new = "%s-%02d" % (VM_ROLES[i % len(VM_ROLES)], i // len(VM_ROLES) + 1)
        elif kind == "host":
            new = "esx%02d.demo.local" % (i + 1)
        elif kind == "cluster":
            new = CLUSTERS[i] if i < len(CLUSTERS) else "Cluster-%d" % (i + 1)
        elif kind == "dc":
            new = "Datacenter-%d" % (i + 1)
        elif kind == "vcenter":
            new = "vcenter01.demo.local"
        else:
            new = "datastore-%02d" % (i + 1)
        m[value] = new
        return new


def scrub(src, out):
    wb = openpyxl.load_workbook(src)
    mapper = Mapper()
    for ws in wb.worksheets:
        if ws.title == "Details":
            for row in ws.iter_rows():
                label = row[0].value
                if label is None or len(row) < 2:
                    continue
                cell = row[1]
                if label == "Project ID":
                    cell.value = 1000001
                elif label == "Project Name":
                    cell.value = "Demo Company assessment"
                elif label == "Owner":
                    cell.value = "demo@example.com"
                elif label == "Date":
                    cell.value = datetime(2026, 9, 1)
            continue
        header = [c.value for c in ws[1]]
        for row in ws.iter_rows(min_row=2):
            for col, cell in zip(header, row):
                v = cell.value
                if not isinstance(v, str) or col is None:
                    continue
                if col in KIND:
                    cell.value = mapper(KIND[col], v)
                elif col == "Disk":
                    # a guest drive letter is harmless; a mount path can name
                    # the customer's software, so it becomes a generic one
                    cell.value = v if re.match(r"^[A-Z]:\\$", v) else "/data"
                elif DROP.match(col):
                    cell.value = None
                elif col not in KEEP:
                    cell.value = "—"
    wb.save(out)
    return {k: len(v) for k, v in mapper.maps.items()}


if __name__ == "__main__":
    print(scrub(sys.argv[1], sys.argv[2]))
