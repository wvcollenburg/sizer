"""Small synthetic assessment workbooks for the multi-file import tests.

Each builder writes an .xlsx with the sheet names and headers the real
parser reads (rvtools.py, liveoptics.py), so the files go through the real
detection and parsing. Figures are chosen to make the expected merge maths
easy to state in a test.
"""
from openpyxl import Workbook


def _save(path, sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return str(path)


def vm(name, host, cluster="", vcpus=4, ram_gb=16, used_gb=100, on=True, uuid=None,
       bios=None):
    return {"name": name, "host": host, "cluster": cluster, "vcpus": vcpus,
            "ram_gb": ram_gb, "used_gb": used_gb, "on": on, "uuid": uuid, "bios": bios}


def host(name, cluster="", cores=16, ghz=2.5, ram_gb=256, cpu_pct=40.0):
    return {"name": name, "cluster": cluster, "cores": cores, "ghz": ghz,
            "ram_gb": ram_gb, "cpu_pct": cpu_pct}


def rvtools(path, hosts, vms, datastores, date="2026/09/01 10:00:00"):
    """datastores: [(name, capacity_gib, used_gib)]"""
    mib = 1024
    return _save(path, {
        "vInfo": (
            ["VM", "Powerstate", "Template", "CPUs", "Memory", "Provisioned MiB",
             "In Use MiB", "Total disk capacity MiB", "Active Memory",
             "OS according to the configuration file", "Host", "Cluster",
             "VM UUID", "SMBIOS UUID"],
            [[v["name"], "poweredOn" if v["on"] else "poweredOff", "FALSE", v["vcpus"],
              v["ram_gb"] * mib, v["used_gb"] * 2 * mib, v["used_gb"] * mib,
              v["used_gb"] * 2 * mib, v["ram_gb"] * mib / 2,
              "Microsoft Windows Server 2019 (64-bit)", v["host"], v["cluster"],
              v["uuid"], v["bios"]] for v in vms]),
        "vHost": (
            ["Host", "Cluster", "Vendor", "Model", "# CPU", "Cores per CPU", "# Cores",
             "HT Active", "Speed", "# Memory", "CPU Model", "# VMs", "# NICs",
             "CPU usage %", "Memory usage %"],
            [[h["name"], h["cluster"], "Dell", "R740", 2, h["cores"] // 2, h["cores"],
              "True", h["ghz"] * 1000, h["ram_gb"] * mib, "Intel Xeon Gold 6230", 0, 2,
              h["cpu_pct"], 50.0] for h in hosts]),
        "vDatastore": (
            ["Name", "Capacity MiB", "In Use MiB", "Free MiB", "# VMs"],
            [[n, c * mib, u * mib, (c - u) * mib, 1] for n, c, u in datastores]),
        "vNIC": (["Host", "Network Device", "Speed", "Driver"],
                 [[h["name"], "vmnic0", 10000, "i40en"] for h in hosts]),
        "vMetaData": (["RVTools major version", "RVTools version", "xlsx creation datetime",
                       "Server"], [[4, "4.6.1", date, "vcenter.local"]]),
    })


def liveoptics(path, hosts, vms, datastores, date="2026/09/07", iops_per_host=1000.0):
    """datastores: [(name, capacity_gib, used_gib, [mounting host names])].
    Every host gets perf: CPU at its cpu_pct, IOPS ``iops_per_host``."""
    kib = 1048576
    rows_dev = []
    for n, cap, used, mounts in datastores:
        for m in mounts:
            rows_dev.append([m, n, "Cluster", cap, used, cap - used, len(vms)])
    return _save(path, {
        "Details": (["Project ID", 1], [["Date", date], ["Project Name", "Synthetic"]]),
        "ESX Hosts": (
            ["Host Name", "Cluster", "Manufacturer", "Model", "CPU Sockets", "CPU Cores",
             "CPU Threads", "CPU Description", "CPU Clock Speed (GHz)",
             "Net Clock Speed (GHz)", "Memory (KiB)", "Local Capacity (GiB)",
             "Guest VM Count", "Number of NICs"],
            [[h["name"], h["cluster"], "Dell", "R750", 2, h["cores"], h["cores"] * 2,
              "Intel Xeon Gold 6338", h["ghz"], h["ghz"] * h["cores"],
              h["ram_gb"] * kib, 0, 0, 2] for h in hosts]),
        "ESX Performance": (
            ["Host", "Peak CPU %", "Peak CPU (GHz)", "Average CPU %", "Average CPU (GHz)",
             "Peak Memory %", "Peak Memory (MiB)", "Average Memory %",
             "Average Memory (MiB)", "Peak IOPS", "Average IOPS", "95% IOPS",
             "Peak Throughput MB/s", "Avg Throughput MB/s"],
            [[h["name"], h["cpu_pct"], round(h["ghz"] * h["cores"] * h["cpu_pct"] / 100, 1),
              h["cpu_pct"] / 2, round(h["ghz"] * h["cores"] * h["cpu_pct"] / 200, 1),
              60.0, h["ram_gb"] * 600, 50.0, h["ram_gb"] * 500,
              iops_per_host * 2, iops_per_host / 2, iops_per_host, 100, 50]
             for h in hosts]),
        "Host Devices": (
            ["Server Name", "Device Name", "Device Type", "Capacity (GiB)",
             "Used Capacity (GiB)", "Free Capacity (GiB)", "VM Count"], rows_dev),
        "VMs": (
            ["VM Name", "Power State", "Template", "Virtual CPU",
             "Provisioned Memory (MiB)", "Used Memory (active) (MiB)",
             "Consumed Memory (MiB)", "Guest VM Disk Capacity (MiB)",
             "Guest VM Disk Used (MiB)", "Virtual Disk Size (MiB)",
             "Virtual Disk Used (MiB)", "VM OS", "Datastore", "Host", "Cluster",
             "UUID", "InstanceUUID"],
            [[v["name"], "poweredOn" if v["on"] else "poweredOff", "FALSE", v["vcpus"],
              v["ram_gb"] * 1024, v["ram_gb"] * 512, v["ram_gb"] * 768,
              v["used_gb"] * 2048, v["used_gb"] * 1024, v["used_gb"] * 2048,
              v["used_gb"] * 1024, "Ubuntu Linux (64-bit)", "ds", v["host"], v["cluster"],
              v["bios"], v["uuid"]] for v in vms]),
        "Host Network Adapters": (
            ["Server Name", "PNIC Name", "Speed (Mbps)", "PNIC Vendor", "PNIC Device Name"],
            [[h["name"], "vmnic0", 25000, "Intel", "E810"] for h in hosts]),
    })


def dataset(client, path, name=None):
    """Upload one file in raw mode and return the response as a dataset."""
    import io
    with open(path, "rb") as fh:
        body = fh.read()
    r = client.post("/api/import-liveoptics?raw=1", data={
        "file": (io.BytesIO(body), name or path.rsplit("/", 1)[-1])},
        content_type="multipart/form-data")
    return r
