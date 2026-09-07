"""Seed catalog for the appliance models.

NOTE ON `cpu_options` ORDER — it is NOT sorted by core count, and must not be
assumed to be. 8 of 43 models list a larger CPU before a smaller one (e.g.
HC1450D: 24C, 16C, 32C, 48C, 64C). Nothing depends on the order: the engine
re-sorts by effective cores in `recommend._fit_model`, and the live catalog
orders by `ModelCpuOption.sort_order` in the database rather than by this list.

Do not "tidy" these lists into ascending order. This list only seeds a FRESH
database, so re-sorting here would make new deployments disagree with existing
ones, and re-sorting `sort_order` in an existing database would renumber
`cpu_index` — which saved sizings and /api/calculate both pass by position.
Any weighting that needs CPU size must read `cores`, never the index; see
docs/pricebook-plan.md §7.4.
"""

APPLIANCE_MODELS = {
    # ==================== EDGE / SFF MODELS ====================
    "HE150": {
        "status": "EOS",
        "category": "1XX SFF",
        "form_factor": "SFF",
        "chassis": "Intel NUC",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i3-10110U 2C/4T 2.1GHz/4.1GHz", "cores": 2, "threads": 4, "ghz": 2.1},
            {"desc": "1 x i5-10210U 4C/8T 1.6GHz/4.2GHz", "cores": 4, "threads": 8, "ghz": 1.6},
            {"desc": "1 x i7-10710U 6C/12T 1.1GHz/4.7GHz", "cores": 6, "threads": 12, "ghz": 1.1},
        ],
        "ram_options_gb": [8, 16, 32, 64],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.25, 0.5, 1, 2, 4, 8],
            "drives_per_node": 1,
        },
        "nic_options": [
            {"desc": "1 x 1GbE 1-port Network Card (Intel i219)", "ports": 1, "speed": "1GbE"},
        ],
        "psu": "1x 120W",
    },
    "HE151": {
        "status": "EOS",
        "category": "1XX SFF",
        "form_factor": "SFF",
        "chassis": "Intel NUC",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i5-1145G7 4C/8T 2.6GHz 3200MT/s vPro", "cores": 4, "threads": 8, "ghz": 2.6},
            {"desc": "1 x i7-1185G7 4C/8T 3.0GHz 3200MT/s vPro", "cores": 4, "threads": 8, "ghz": 3.0},
        ],
        "ram_options_gb": [8, 16, 32, 64],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.25, 0.5, 1, 2, 4, 8],
            "drives_per_node": 1,
        },
        "nic_options": [
            {"desc": "2 x 2.5GbE 1-port Network Cards (Intel i225)", "ports": 2, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
    },
    "HE153": {
        "status": "EOS",
        "category": "1XX SFF",
        "form_factor": "SFF NUC",
        "chassis": "Asus NUC13L3Hv5 or NUC13L3Hv7",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i7-1370P 6PC/8E 1.9GHz(5.2GHz)", "cores": 14, "threads": 20, "ghz": 1.9},
        ],
        "ram_options_gb": [16, 32, 64],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1, 2, 4, 8],
            "drives_per_node": 1,
        },
        "nic_options": [
            {"desc": "2 x 2.5GbE 1-port Network Cards (Intel i226-LM(V))", "ports": 2, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
    },
    "HE153s": {
        "status": "EOS",
        "category": "1XX SFF",
        "form_factor": "SFF NUC",
        "chassis": "Asus NUC13L3Kv5 or NUC13L3Kv7",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i7-1370P 6PC/8E 1.9GHz(5.2GHz)", "cores": 14, "threads": 20, "ghz": 1.9},
        ],
        "ram_options_gb": [16, 32, 64],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1, 2, 4, 8],
            "drives_per_node": 1,
        },
        "nic_options": [
            {"desc": "1 x 2.5GbE 1-port Network Cards (Intel i226-LM)", "ports": 1, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
    },
    "HE153p": {
        "status": "Active",
        "category": "1XX SFF",
        "form_factor": "SFF Rack Mountable",
        "chassis": "SimplyNUC Onyx i9",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i9-13900H 6PC/8E 2.6GHz(5.4GHz)", "cores": 14, "threads": 20, "ghz": 2.6},
        ],
        "ram_options_gb": [16, 32, 64, 96],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1, 2, 4, 8],
            "drives_per_node": 1,
        },
        "nic_options": [
            {"desc": "2 x 2.5GbE 1-port Network Cards (Intel i226-LM(V))", "ports": 2, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
        "notes": "3 nodes per 2U rack mount. SC//Hypercore licensing based on Performance Cores.",
    },
    "HE155-1": {
        "status": "Active",
        "category": "1XX SFF",
        "form_factor": "SFF",
        "chassis": "TBD",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Ultra 5 225H 4PC/8E 1.7GHz(4.9GHz)", "cores": 14, "threads": 14, "ghz": 1.7},
            {"desc": "1 x Ultra 7 255H 6PC/8E 2.0GHz(5.1GHz)", "cores": 16, "threads": 16, "ghz": 2.0},
        ],
        "ram_options_gb": [16, 32, 64, 96],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_and_ssd",
            "nvme_options_tb": [1, 2, 4, 8],
            "ssd_options_tb": [0.96, 1.92, 3.84, 7.68],
            "drives_per_node": 2,
        },
        "nic_options": [
            {"desc": "1x 2.5GbE 2-port Network Cards (Intel i226-V)", "ports": 2, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
        "min_nodes": 3,
    },
    "HE155-2": {
        "status": "Active",
        "category": "1XX SFF",
        "form_factor": "SFF",
        "chassis": "TBD",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Ultra 5 235H 4PC/8E 2.4GHz(5.0GHz)", "cores": 14, "threads": 14, "ghz": 2.4},
            {"desc": "1 x Ultra 7 265H 6PC/8E 2.2GHz(5.3GHz)", "cores": 16, "threads": 16, "ghz": 2.2},
        ],
        "ram_options_gb": [16, 32, 64, 96],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1, 2, 4, 8],
            "drives_per_node": 2,
        },
        "nic_options": [
            {"desc": "2 x 2.5GbE 2-port Network Cards (OB+Intel i226-V)", "ports": 4, "speed": "2.5GbE"},
        ],
        "psu": "1x 120W",
        "min_nodes": 3,
    },
    "HE250": {
        "status": "Active",
        "category": "2XX SFF",
        "form_factor": "SFF Rack Mountable",
        "chassis": "SimplyNUC Onyx Pro (MS-01)",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x i9-13900H 6PC/8E 2.6GHz(5.4GHz)", "cores": 14, "threads": 20, "ghz": 2.6},
        ],
        "ram_options_gb": [16, 32, 64, 96],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1, 2, 4, 8],
            "drives_per_node": 3,
        },
        "nic_options": [
            {"desc": "1 x 2.5GbE/10GbE 2-port each (Intel i226-LM/V + Intel X710-DA2)", "ports": 4, "speed": "2.5GbE/10GbE"},
        ],
        "psu": "1x 180W",
    },
    "SE100": {
        "status": "Active",
        "category": "1XX SFF",
        "form_factor": "SFF",
        "chassis": "Lenovo SE100",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Ultra 5 225H 4PC/8E 1.7GHz(4.9GHz)", "cores": 14, "threads": 14, "ghz": 1.7},
            {"desc": "1 x Ultra 7 255H 6PC/8E 2.0GHz(5.1GHz)", "cores": 16, "threads": 16, "ghz": 2.0},
        ],
        "ram_options_gb": [16, 32, 64],
        "ram_slots": 2,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.48, 0.96, 1.92],
            "drives_per_node": 2,
        },
        "nic_options": [
            {"desc": "1 x 1GbE 2-port Network Card (Broadcom BCM5720)", "ports": 2, "speed": "1GbE"},
        ],
        "psu": "1x 140W",
    },

    # ==================== 1U SINGLE SOCKET ====================
    "HE500": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2224 @ 3.4GHz", "cores": 4, "threads": 4, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2234 @ 3.6GHz", "cores": 4, "threads": 8, "ghz": 3.6},
            {"desc": "1 x Intel Xeon E-2236 @ 3.4 GHz", "cores": 6, "threads": 12, "ghz": 3.4},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [2, 4, 8],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 1GbE 2-port Network Card (Intel i350)", "ports": 2, "speed": "1GbE"},
            {"desc": "1 x 1GbE 4-port Network Card (Intel i350)", "ports": 4, "speed": "1GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X710)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE501": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2324G @ 3.1GHz", "cores": 4, "threads": 4, "ghz": 3.1},
            {"desc": "1 x Intel Xeon E-2334 @ 3.4GHz", "cores": 4, "threads": 8, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2386G @ 3.5 GHz", "cores": 6, "threads": 12, "ghz": 3.5},
            {"desc": "1 x Intel Xeon E-2388G @ 3.2 GHz", "cores": 8, "threads": 16, "ghz": 3.2},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [4, 8, 12],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 2-port Network Card (Intel X710-T2L)", "ports": 2, "speed": "10GbE"},
            {"desc": "2 x 10GbE SFP+ 2-port Network Cards (Intel X710-DA2)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE502": {
        "status": "Active",
        "category": "5XX Edge",
        "form_factor": "1U Rack (1.7in)",
        "chassis": "Supermicro SYS-511R-M",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2414 @ 4.2 GHz", "cores": 4, "threads": 4, "ghz": 4.2},
            {"desc": "1 x Intel Xeon E-2434 @ 4.6 GHz", "cores": 4, "threads": 8, "ghz": 4.6},
            {"desc": "1 x Intel Xeon E-2436 @ 4.4 GHz", "cores": 6, "threads": 12, "ghz": 4.4},
            {"desc": "1 x Intel Xeon E-2468 @ 4.4 GHz", "cores": 8, "threads": 16, "ghz": 4.4},
        ],
        "ram_options_gb": [32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [4, 8, 12, 16],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-Port Network Card (Intel XL710+X557)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP28 4-Port Network Cards (Intel E810-XXVDA4)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W Titanium",
    },
    "HE550": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2224 @ 3.4GHz", "cores": 4, "threads": 4, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2234 @ 3.6GHz", "cores": 4, "threads": 8, "ghz": 3.6},
            {"desc": "1 x Intel Xeon E-2236 @ 3.4 GHz", "cores": 6, "threads": 12, "ghz": 3.4},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [2, 4, 8],
            "ssd_options_tb": [0.24, 0.48, 0.96, 1.92, 3.84],
            "hdd_count": 3,
            "ssd_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 1GbE 2-port Network Card (Intel i350)", "ports": 2, "speed": "1GbE"},
            {"desc": "1 x 1GbE 4-port Network Card (Intel i350)", "ports": 4, "speed": "1GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X710)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE551": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2324G @ 3.1GHz", "cores": 4, "threads": 4, "ghz": 3.1},
            {"desc": "1 x Intel Xeon E-2334 @ 3.4GHz", "cores": 4, "threads": 8, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2386G @ 3.5 GHz", "cores": 6, "threads": 12, "ghz": 3.5},
            {"desc": "1 x Intel Xeon E-2388G @ 3.2 GHz", "cores": 8, "threads": 16, "ghz": 3.2},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [4, 8, 12],
            "ssd_options_tb": [0.48, 0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "ssd_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 2-port Network Card (Intel X710-T2L)", "ports": 2, "speed": "10GbE"},
            {"desc": "2 x 10GbE SFP+ 2-port Network Cards (Intel X710-DA2)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE552": {
        "status": "Active",
        "category": "5XX Edge",
        "form_factor": "1U Rack (1.7in)",
        "chassis": "Supermicro SYS-511R-M",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2414 @ 4.2 GHz", "cores": 4, "threads": 4, "ghz": 4.2},
            {"desc": "1 x Intel Xeon E-2434 @ 4.6 GHz", "cores": 4, "threads": 8, "ghz": 4.6},
            {"desc": "1 x Intel Xeon E-2436 @ 4.4 GHz", "cores": 6, "threads": 12, "ghz": 4.4},
            {"desc": "1 x Intel Xeon E-2468 @ 4.4 GHz", "cores": 8, "threads": 16, "ghz": 4.4},
        ],
        "ram_options_gb": [32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [4, 8, 12, 16],
            "ssd_options_tb": [0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "ssd_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-Port Network Card (Intel XL710+X557)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP28 4-Port Network Cards (Intel E810-XXVDA4)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W Titanium",
    },
    "HE550F": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2224 @ 3.4GHz", "cores": 4, "threads": 4, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2234 @ 3.6GHz", "cores": 4, "threads": 8, "ghz": 3.6},
            {"desc": "1 x Intel Xeon E-2236 @ 3.4 GHz", "cores": 6, "threads": 12, "ghz": 3.4},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "ssd_only",
            "ssd_options_tb": [0.24, 0.48, 0.96, 1.92, 3.84],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 1GbE 2-port Network Card (Intel i350)", "ports": 2, "speed": "1GbE"},
            {"desc": "1 x 1GbE 4-port Network Card (Intel i350)", "ports": 4, "speed": "1GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X710)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE551F": {
        "status": "EOS",
        "category": "5XX Edge",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2324G @ 3.1GHz", "cores": 4, "threads": 4, "ghz": 3.1},
            {"desc": "1 x Intel Xeon E-2334 @ 3.4GHz", "cores": 4, "threads": 8, "ghz": 3.4},
            {"desc": "1 x Intel Xeon E-2386G @ 3.5 GHz", "cores": 6, "threads": 12, "ghz": 3.5},
            {"desc": "1 x Intel Xeon E-2388G @ 3.2 GHz", "cores": 8, "threads": 16, "ghz": 3.2},
        ],
        "ram_options_gb": [16, 32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "ssd_only",
            "ssd_options_tb": [0.48, 0.96, 1.92, 3.84, 7.68],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 2-port Network Card (Intel X710-T2L)", "ports": 2, "speed": "10GbE"},
            {"desc": "2 x 10GbE SFP+ 2-port Network Cards (Intel X710-DA2)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W",
    },
    "HE552F": {
        "status": "Active",
        "category": "5XX Edge",
        "form_factor": "1U Rack (1.7in)",
        "chassis": "Supermicro SYS-511R-M",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Intel Xeon E-2414 @ 4.2 GHz", "cores": 4, "threads": 4, "ghz": 4.2},
            {"desc": "1 x Intel Xeon E-2434 @ 4.6 GHz", "cores": 4, "threads": 8, "ghz": 4.6},
            {"desc": "1 x Intel Xeon E-2436 @ 4.4 GHz", "cores": 6, "threads": 12, "ghz": 4.4},
            {"desc": "1 x Intel Xeon E-2468 @ 4.4 GHz", "cores": 8, "threads": 16, "ghz": 4.4},
        ],
        "ram_options_gb": [32, 64, 128],
        "ram_slots": 4,
        "storage": {
            "type": "ssd_only",
            "ssd_options_tb": [0.96, 1.92, 3.84, 7.68],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-Port Network Card (Intel XL710+X557)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP28 4-Port Network Cards (Intel E810-XXVDA4)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 600W Titanium",
    },

    # ==================== 1U DATACENTER SINGLE SOCKET ====================
    "HC1200": {
        "status": "EOS",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Bronze 3204 6C/6T 1.9Ghz", "cores": 6, "threads": 6, "ghz": 1.9},
            {"desc": "1 x Silver 4208 8C/16T 2.1Ghz", "cores": 8, "threads": 16, "ghz": 2.1},
        ],
        "ram_options_gb": [64, 96, 128, 192, 256, 384],
        "ram_slots": 6,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 500W",
    },
    "HC1300": {
        "status": "EOL",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro SYS-510P-WTR",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 6326 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512],
        "ram_slots": 8,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X550-T2)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 500W",
    },
    "HC1400": {
        "status": "EOL",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro SYS-511E-WR",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Silver 4410Y 12C/24T 2.8GHz", "cores": 12, "threads": 24, "ghz": 2.8},
            {"desc": "1 x Gold 5415+ 8C/16T 3.6GHz", "cores": 8, "threads": 16, "ghz": 3.6},
            {"desc": "1 x Gold 6426Y 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
            {"desc": "1 x Gold 5418Y 24C/48T 2.8GHz", "cores": 24, "threads": 48, "ghz": 2.8},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512],
        "ram_slots": 8,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [4, 8, 12, 16, 20],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L OCP)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC1600": {
        "status": "Active",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Xeon 6505P 12C/24T 3.2GHz(3.9GHz)", "cores": 12, "threads": 24, "ghz": 2.2},
            {"desc": "1 x Xeon 6517P 16C/32T 3.6GHz(4.0GHz)", "cores": 16, "threads": 32, "ghz": 3.2},
        ],
        "ram_options_gb": [256, 512],
        "ram_slots": 8,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [8, 16, 20],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom 57504 OCP)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },

    # ==================== 1U DATACENTER DUAL SOCKET ====================
    "HC1250": {
        "status": "EOS",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "dual",
        "cpu_options": [
            {"desc": "1 x Silver 4208 8C/16T 2.1GHz", "cores": 8, "threads": 16, "ghz": 2.1},
            {"desc": "1 x Silver 4215R 8C/16T, 3.2 GHz", "cores": 8, "threads": 16, "ghz": 3.2},
            {"desc": "1 x Silver 4210R 10C/20T, 2.4 GHz", "cores": 10, "threads": 20, "ghz": 2.4},
            {"desc": "1 x Gold 6226R 16C/32T, 2.9 GHz", "cores": 16, "threads": 32, "ghz": 2.9},
            {"desc": "1 x Gold 6226 12C/24T, 2.7 GHz", "cores": 12, "threads": 24, "ghz": 2.7},
        ],
        "ram_options_gb": [64, 96, 128, 192, 256, 384],
        "ram_slots": 6,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "ssd_options_tb": [0.48, 0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "ssd_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 500W",
    },
    "HC1250D": {
        "status": "EOS",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Silver 4208 8C/16T 2.1GHz", "cores": 16, "threads": 32, "ghz": 2.1},
            {"desc": "2 x Silver 4215R 8C/16T, 3.2 GHz", "cores": 16, "threads": 32, "ghz": 3.2},
            {"desc": "2 x Silver 4210R 10C/20T, 2.4 GHz", "cores": 20, "threads": 40, "ghz": 2.4},
            {"desc": "2 x Gold 5218R 20C/40T, 2.1 GHz", "cores": 40, "threads": 80, "ghz": 2.1},
            {"desc": "2 x Gold 6226R 16C/32T, 2.9 GHz", "cores": 32, "threads": 64, "ghz": 2.9},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768],
        "ram_slots": 12,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "ssd_options_tb": [0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "ssd_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 500W",
    },
    "HC1350": {
        "status": "EOL",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro SYS-510P-WTR",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 6326 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512],
        "ram_slots": 8,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "nvme_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X550-T2)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 500W",
    },
    "HC1450": {
        "status": "EOL",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro SYS-511E-WR",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Silver 4410Y 12C/24T 2.8GHz", "cores": 12, "threads": 24, "ghz": 2.8},
            {"desc": "1 x Gold 5415+ 8C/16T 3.6GHz", "cores": 8, "threads": 16, "ghz": 3.6},
            {"desc": "1 x Gold 6426Y 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
            {"desc": "1 x Gold 5418Y 24C/48T 2.8GHz", "cores": 24, "threads": 48, "ghz": 2.8},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512],
        "ram_slots": 8,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [4, 8, 12, 16, 20],
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68],
            "hdd_count": 3,
            "nvme_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L OCP)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC1450D": {
        "status": "EOL",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630v3",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Silver 4410Y 12C/24T", "cores": 24, "threads": 48, "ghz": 2.8},
            {"desc": "2 x Gold 5415+ 8C/16T 3.6GHz", "cores": 16, "threads": 32, "ghz": 3.6},
            {"desc": "2 x Gold 6426Y 16C/32T 3.3GHz", "cores": 32, "threads": 64, "ghz": 3.3},
            {"desc": "2 x Gold 5418Y 24C/48T 2.8GHz", "cores": 48, "threads": 96, "ghz": 2.8},
            {"desc": "2 x Gold 6438N 32C/64T 2.7GHz", "cores": 64, "threads": 128, "ghz": 2.7},
        ],
        "ram_options_gb": [256, 384, 512, 768, 1024, 1536, 2048],
        "ram_slots": 32,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [4, 8, 12, 16, 20],
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "hdd_count": 3,
            "nvme_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L OCP)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC1650D": {
        "status": "Active",
        "category": "1XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Supermicro",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Xeon 6505P 12C/24T 3.2GHz(3.9GHz)", "cores": 24, "threads": 48, "ghz": 2.2},
            {"desc": "2 x Xeon 6517P 16C/32T 3.6GHz(4.0GHz)", "cores": 32, "threads": 64, "ghz": 3.2},
        ],
        "ram_options_gb": [256, 512],
        "ram_slots": 16,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [8, 16, 20],
            "nvme_options_tb": [3.84, 7.68],
            "hdd_count": 3,
            "nvme_count": 1,
        },
        "nic_options": [
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom 57504 OCP)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },

    # ==================== 1U ALL-FLASH (3xxx SERIES) ====================
    "HC3250DF": {
        "status": "EOS",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Dell R650",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Silver 4215R 8C/16T 3.2GHz", "cores": 16, "threads": 32, "ghz": 3.2},
            {"desc": "2 x Gold 6226R 16C/32T 2.9GHz", "cores": 32, "threads": 64, "ghz": 2.9},
            {"desc": "2 x Gold 6230R 26C/52T 2.1GHz", "cores": 52, "threads": 104, "ghz": 2.1},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024, 1536],
        "ram_slots": 24,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68],
            "drives_per_node": 10,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Cards (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 25GbE 2-port Network Card (ConnectX-4 Lx)", "ports": 2, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3350F": {
        "status": "EOS",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Dell R650",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 5315Y 8C/16T 3.5GHz", "cores": 8, "threads": 16, "ghz": 3.5},
            {"desc": "1 x Gold 5317 12C/24T 3.4GHz", "cores": 12, "threads": 24, "ghz": 3.4},
            {"desc": "1 x Gold 6326 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
            {"desc": "1 x Gold 6336Y 24C/48T 3.0GHz", "cores": 24, "threads": 48, "ghz": 3.0},
            {"desc": "1 x Gold 6338N 32C/64T 2.7GHz", "cores": 32, "threads": 64, "ghz": 2.7},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024],
        "ram_slots": 16,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3350DF": {
        "status": "EOS",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Dell R650",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Gold 5315Y 8C/16T 3.5GHz", "cores": 16, "threads": 32, "ghz": 3.5},
            {"desc": "2 x Gold 5317 12C/24T 3.4GHz", "cores": 24, "threads": 48, "ghz": 3.4},
            {"desc": "2 x Gold 6326 16C/32T 3.3GHz", "cores": 32, "threads": 64, "ghz": 3.3},
            {"desc": "2 x Gold 6336Y 24C/48T 3.0GHz", "cores": 48, "threads": 96, "ghz": 3.0},
            {"desc": "2 x Gold 6338N 32C/64T 2.7GHz", "cores": 64, "threads": 128, "ghz": 2.7},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024, 1536, 2048],
        "ram_slots": 32,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "drives_per_node": 10,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3450F": {
        "status": "Active",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630v3",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 5515+ 8C/16T 3.6GHz", "cores": 8, "threads": 16, "ghz": 3.6},
            {"desc": "1 x Gold 6526Y 16C/32T 3.5GHz", "cores": 16, "threads": 32, "ghz": 3.5},
            {"desc": "1 x Silver 4516Y+ 24C/48T 2.9GHz", "cores": 24, "threads": 48, "ghz": 2.9},
            {"desc": "1 x Gold 5520+ 28C/56T 3.0GHz", "cores": 28, "threads": 56, "ghz": 3.0},
            {"desc": "1 x Gold 6538N 32C/64T 2.9GHz", "cores": 32, "threads": 64, "ghz": 2.9},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024],
        "ram_slots": 16,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3450DF": {
        "status": "Active",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630v3",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Gold 5515+ 8C/16T 3.6GHz", "cores": 16, "threads": 32, "ghz": 3.6},
            {"desc": "2 x Gold 6526Y 16C/32T 3.5GHz", "cores": 32, "threads": 64, "ghz": 3.5},
            {"desc": "2 x Silver 4516Y+ 24C/48T 2.9GHz", "cores": 48, "threads": 96, "ghz": 2.9},
            {"desc": "2 x Gold 5520+ 28C/56T 3.0GHz", "cores": 56, "threads": 112, "ghz": 3.0},
            {"desc": "2 x Gold 6538N 32C/64T 2.9GHz", "cores": 64, "threads": 128, "ghz": 2.9},
        ],
        "ram_options_gb": [256, 384, 512, 768, 1024, 1536, 2048],
        "ram_slots": 32,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "drives_per_node": 10,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3450FG": {
        "status": "Active",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630v3",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 5515+ 8C/16T 3.6GHz", "cores": 8, "threads": 16, "ghz": 3.6},
            {"desc": "1 x Gold 6526Y 16C/32T 3.5GHz", "cores": 16, "threads": 32, "ghz": 3.5},
            {"desc": "1 x Gold 6542Y 24C/48T 3.6GHz", "cores": 24, "threads": 48, "ghz": 3.6},
            {"desc": "1 x Gold 6548N 32C/64T 3.5GHz", "cores": 32, "threads": 64, "ghz": 3.5},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024],
        "ram_slots": 16,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1.92, 3.84, 7.68, 15.36],
            "drives_per_node": 4,
        },
        "gpu": "2 x Nvidia L4 24GB",
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC3650F": {
        "status": "Active",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630V4",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Gold 6507P 8C/16T 4.3GHz", "cores": 8, "threads": 16, "ghz": 4.3},
            {"desc": "1 x Gold 6517P 16C/32T 4.0GHz", "cores": 16, "threads": 32, "ghz": 4.0},
            {"desc": "1 x Gold 6520P 24C/48T 3.4GHz", "cores": 24, "threads": 48, "ghz": 3.4},
            {"desc": "1 x Gold 6530P 32C/64T 3.6GHz", "cores": 32, "threads": 64, "ghz": 3.7},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024, 1536],
        "ram_slots": 16,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1.92, 3.84, 7.68, 15.36, 30.72],
            "drives_per_node": 4,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel E610-XT4)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom BCM57504)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1300W",
    },
    "HC3650DF": {
        "status": "Active",
        "category": "3XXX Core",
        "form_factor": "1U Rack",
        "chassis": "Lenovo SR630V4",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Gold 6507P 8C/16T 4.3GHz", "cores": 16, "threads": 32, "ghz": 4.3},
            {"desc": "2 x Gold 6517P 16C/32T 4.0GHz", "cores": 32, "threads": 64, "ghz": 4.0},
            {"desc": "2 x Gold 6520P 24C/48T 3.4GHz", "cores": 48, "threads": 96, "ghz": 3.4},
            {"desc": "2 x Gold 6530P 32C/64T 3.6GHz", "cores": 64, "threads": 128, "ghz": 3.7},
        ],
        "ram_options_gb": [256, 384, 512, 768, 1024, 1536, 2048, 3072],
        "ram_slots": 32,
        "storage": {
            "type": "nvme_only",
            "nvme_options_tb": [1.92, 3.84, 7.68, 15.36, 30.72],
            "drives_per_node": 10,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel E610-XT4)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom BCM57504)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1300W",
    },

    # ==================== 2U DATACENTER ====================
    "HC5200": {
        "status": "EOS",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Supermicro",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Silver 4208 8C/16T 2.1GHz", "cores": 8, "threads": 16, "ghz": 2.1},
            {"desc": "1 x Silver 4215R 8C/16T 3.2GHz", "cores": 8, "threads": 16, "ghz": 3.2},
            {"desc": "1 x Silver 4210R 10C/20T, 2.4 GHz", "cores": 10, "threads": 20, "ghz": 2.4},
            {"desc": "1 x Gold 6226R 16C/32T 2.9GHz", "cores": 16, "threads": 32, "ghz": 2.9},
        ],
        "ram_options_gb": [64, 96, 128, 192, 256, 384, 512, 768],
        "ram_slots": 12,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [8, 12, 16],
            "drives_per_node": 12,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC5250D": {
        "status": "EOS",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Supermicro",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Silver 4208 8C/16T 2.1GHz", "cores": 16, "threads": 32, "ghz": 2.1},
            {"desc": "2 x Silver 4210R 10C/20T, 2.4 GHz", "cores": 20, "threads": 40, "ghz": 2.4},
            {"desc": "2 x Silver 4215R 8C/16T 3.2GHz", "cores": 16, "threads": 32, "ghz": 3.2},
            {"desc": "2 x Gold 6230R 26C/52T 2.1GHz", "cores": 52, "threads": 104, "ghz": 2.1},
            {"desc": "2 x Gold 6226R 16C/32T 2.9GHz", "cores": 32, "threads": 64, "ghz": 2.9},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024, 1536],
        "ram_slots": 24,
        "storage": {
            "type": "hybrid",
            "hdd_options_tb": [2, 4, 8, 12, 16],
            "ssd_options_tb": [0.96, 1.92, 3.84, 7.68],
            "hdd_count": 9,
            "ssd_count": 3,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10GbE SFP+ 4-port Network Card (Intel X722)", "ports": 4, "speed": "10GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC5400": {
        "status": "Active",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Lenovo SR650v3",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Silver 4410Y 12C/24T 2.8GHz", "cores": 12, "threads": 24, "ghz": 2.8},
            {"desc": "1 x Gold 5415+ 8C/16T 3.6GHz", "cores": 8, "threads": 16, "ghz": 3.6},
            {"desc": "1 x Gold 6426Y 16C/32T 3.3GHz", "cores": 16, "threads": 32, "ghz": 3.3},
            {"desc": "1 x Gold 5418Y 24C/48T 2.8GHz", "cores": 24, "threads": 48, "ghz": 2.8},
            {"desc": "1 x Gold 6438N 32C/64T 2.7GHz", "cores": 32, "threads": 64, "ghz": 2.7},
        ],
        "ram_options_gb": [128, 192, 256, 384, 512, 768, 1024],
        "ram_slots": 16,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [8, 12, 16, 20],
            "drives_per_node": 12,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port OCP Network Card (Intel X710-T4L)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC5450D": {
        "status": "Active",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Lenovo SR650v3",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Silver 4410Y 12C/24T", "cores": 24, "threads": 48, "ghz": 2.8},
            {"desc": "2 x Gold 5415+ 8C/16T 3.6GHz", "cores": 16, "threads": 32, "ghz": 3.6},
            {"desc": "2 x Gold 6426Y 16C/32T 3.3GHz", "cores": 32, "threads": 64, "ghz": 3.3},
            {"desc": "2 x Gold 5418Y 24C/48T 2.8GHz", "cores": 48, "threads": 96, "ghz": 2.8},
            {"desc": "2 x Gold 6438N 32C/64T 2.7GHz", "cores": 64, "threads": 128, "ghz": 2.7},
        ],
        "ram_options_gb": [256, 384, 512, 768, 1024, 1536, 2048],
        "ram_slots": 32,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [4, 8, 12, 16, 20],
            "nvme_options_tb": [0.96, 1.92, 3.84, 7.68, 15.36],
            "hdd_count": 9,
            "nvme_count": 3,
        },
        "nic_options": [
            {"desc": "1 x 10GBase-T 4-port Network Card (Intel X710-T4L OCP)", "ports": 4, "speed": "10GbE"},
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Intel E810-XXVDA4)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1100W",
    },
    "HC5600": {
        "status": "Active",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Lenovo SR650V4",
        "socket": "single",
        "cpu_options": [
            {"desc": "1 x Xeon 6517P 16C/32T 3.6GHz(4.0GHz)", "cores": 16, "threads": 32, "ghz": 3.6},
        ],
        "ram_options_gb": [256],
        "ram_slots": 8,
        "storage": {
            "type": "hdd_only",
            "hdd_options_tb": [16, 20],
            "drives_per_node": 12,
        },
        "nic_options": [
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom 57504 OCP)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1300W",
    },
    "HC5650D": {
        "status": "Active",
        "category": "5XXX Core",
        "form_factor": "2U Rack",
        "chassis": "Lenovo SR650V4",
        "socket": "dual",
        "cpu_options": [
            {"desc": "2 x Xeon 6507P 8C/16T 3.5GHz(4.3GHz)", "cores": 16, "threads": 32, "ghz": 3.5},
            {"desc": "2 x Xeon 6517P 16C/32T 3.6GHz(4.0GHz)", "cores": 32, "threads": 64, "ghz": 3.2},
        ],
        "ram_options_gb": [512, 1024],
        "ram_slots": 16,
        "storage": {
            "type": "hybrid_nvme",
            "hdd_options_tb": [8, 16, 20],
            "nvme_options_tb": [3.84, 7.68],
            "hdd_count": 9,
            "nvme_count": 3,
        },
        "nic_options": [
            {"desc": "1 x 10/25GbE SFP28 4-port Network Card (Broadcom 57504 OCP)", "ports": 4, "speed": "25GbE"},
        ],
        "psu": "2x 1300W",
    },

    # ==================== CLOUD ====================
    "Cloud Unity": {
        "status": "Active",
        "category": "Cloud",
        "form_factor": "Virtual",
        "chassis": "N/A",
        "socket": "virtual",
        "cpu_options": [
            {"desc": "16 Threads", "cores": 0, "threads": 16, "ghz": 0},
            {"desc": "32 Threads", "cores": 0, "threads": 32, "ghz": 0},
            {"desc": "64 Threads", "cores": 0, "threads": 64, "ghz": 0},
        ],
        "ram_options_gb": [124, 252, 416],
        "ram_slots": 0,
        "storage": {
            "type": "cloud",
            "options": [
                "2TB (0.25TB Egress)", "4TB (0.50TB Egress)", "6TB (0.75TB Egress)",
                "8TB (1TB Egress)", "12TB (1.5TB Egress)", "16TB (2TB Egress)",
                "20TB (2.5TB Egress)", "24TB (3TB Egress)", "32TB (4TB Egress)",
                "64TB (8TB Egress)",
            ],
        },
        "nic_options": [{"desc": "Virtual", "ports": 0, "speed": "Virtual"}],
        "psu": "N/A",
        "notes": "1 year term. Annual subscription billed monthly. Requires DR Planning Service QDRPS.",
    },
}

# ── Per-model platform tier (price-proportional) ─────────────────────────────
# Seeded into models.cost_tier (admin-tunable thereafter).
#
# RE-BASED 2026-09-02. This used to be a hand-set *capability* weight whose
# absolute units did not matter. It is now deliberately **proportional to what a
# configured node costs**, because `eur_per_tier_point` bridges it to real
# licence euro in the score — a bridge is meaningless if one side is not a price.
# One tier point ~= EUR 838 (the reference basket median in app/platform_tier.py).
#
# **Anti-sprawl now lives in `node_overhead`, not here.** The old wide spacing
# was doing double duty: price proxy AND a guardrail stopping the ranker
# collapsing a large estate into many tiny appliances. Those roles disagreed by
# up to 3x, so they are now separated. `node_overhead` (a flat, model-independent
# per-node charge) is the knob that penalises node count; raise it if sprawl
# appears. The hard guards — largest-VM RAM fit, min_nodes, the per-cluster disk
# cap, the IOPS gate — still bound the answer regardless.
#
# Derived by scaling each family band by its basket correction factor:
#   1XX / 2XX SFF x0.95   5XX Edge hybrid x0.30   5XX Edge flash x0.40
#   1XXX Core    x1.05    3XXX Core       x1.58   5XXX Core      x1.05
# Spacing WITHIN a band is inherited from the old ladder, so a band whose
# internal spread was wrong stays wrong until corrected explicitly. One such
# correction has been made:
#
#   **Socket deltas are the family's ENTRY CPU price, and they differ by band.**
#   Going from one socket to two adds one more of the cheapest CPU the family
#   offers, so the gap is that CPU's price — not a fixed percentage:
#       1XXX Core  EUR 1,000  (entry is a Xeon 6505P, a P-series volume part)
#       3XXX/5XXX  EUR 1,750  (entry is a Gold 5515+/5415+ — fewer cores, but a
#                              Gold, and Gold costs more)
#   The old ladder had these gaps 3.5x to 8.3x too wide.
#
#   **CAUTION — perf_index is NOT a reliable price proxy at the low end.** The
#   3XXX entry CPU has LOWER SPECrate than the 1XXX entry (88 vs 142) yet costs
#   MORE, because SPECrate measures throughput while Intel's Gold/Silver tier
#   premium is clock and segment. So `w_cpu_perf` systematically underprices
#   low-core high-tier parts. Do not derive socket deltas from perf; ask.
#
#   **HC3450FG carries a DELIBERATE 2x surcharge** (2026-09-02). Its hardware
#   delta over the non-GPU HC3450F is ~EUR 9,700, which is right — that is TWO
#   Nvidia L4 cards, not one. On top of that the tier is doubled to stand in for
#   per-user vGPU licensing, which is steep and which nothing in the engine can
#   see. It deliberately does not look proportionate to the hardware. It is close
#   to the real total cost, and it has a useful side effect: the engine has NO
#   GPU demand signal (nothing reads the `gpu` field), so a GPU node must never
#   win an UNPROMPTED recommendation. An SA who wants one still names it
#   explicitly, which bypasses ranking.
#
#   **1XX / 2XX SFF compressed to 1.9-2.4** (~EUR 1,590-2,010). The old ladder
#   spread this band 2 -> 5, which after re-basing implied EUR 3,934 for an SE100
#   or HE250 — more than a 5XX Edge 1U rack node at EUR 2,500, which is backwards.
#   The whole SFF range is ~EUR 1.5-2k. HE153 stays at 1.9 because that is its
#   real quoted price (EUR 1,588); the rest are scaled onto that range in their
#   original order.
#
# Do not hand-edit a single value to nudge a recommendation. Change it because a
# real configured-node price says so, and re-run tools/license_sweep.py.
MODEL_COSTS = {
    # Edge (SFF / NUC)
    "HE150": 1.9, "HE151": 1.9, "HE153": 1.9, "HE153s": 1.9, "HE153p": 2.07,
    "HE155-1": 2.07, "HE155-2": 2.23, "HE250": 2.4, "SE100": 2.4,
# 1U Rack (hybrid)
    "HE500": 2.4, "HE501": 2.4, "HE502": 2.7, "HE550": 2.7, "HE551": 2.7,
    "HE552": 3,
# 1U Rack (all-flash)
    "HE550F": 4.4, "HE551F": 4.4, "HE552F": 4.8,
# Datacenter 1U single socket (hybrid)
    "HC1200": 17.7, "HC1300": 18.8, "HC1400": 19.8, "HC1600": 24,
    "HC1250": 18.8, "HC1350": 20.9, "HC1450": 21.9,
# Datacenter 1U dual socket (hybrid)
    "HC1250D": 20, "HC1450D": 23.1, "HC1650D": 25.2,
# Datacenter 1U all-flash
    "HC3250DF": 37.9, "HC3350F": 45.3, "HC3350DF": 47.4, "HC3450F": 51.6,
    "HC3450DF": 53.7, "HC3450FG": 126.4, "HC3650F": 57.9, "HC3650DF": 60,
# Datacenter 2U
    "HC5200": 27.4, "HC5250D": 29.5, "HC5400": 33.7, "HC5450D": 35.8,
    "HC5600": 40, "HC5650D": 42.1,
# Cloud (virtual)
    "Cloud Unity": 12,
}

# Fallback cost for any model missing from MODEL_COSTS (mirrors the old
# hardcoded default of 5).
DEFAULT_MODEL_COST = 5

VALIDATED_NICS = [
    {"desc": "Intel X710-T4L (10GBase-T 4-port)", "speed": "10GbE", "ports": 4},
    {"desc": "Intel X710-DA2 (10GbE SFP+ 2-port)", "speed": "10GbE", "ports": 2},
    {"desc": "Intel E810-XXVDA4 (10/25GbE SFP28 4-port)", "speed": "25GbE", "ports": 4},
    {"desc": "Intel E810-XXVDA2 (10/25GbE SFP28 2-port)", "speed": "25GbE", "ports": 2},
    {"desc": "Intel E610-XT4 (10GBase-T 4-port OCP)", "speed": "10GbE", "ports": 4},
    {"desc": "Broadcom BCM57416 (10GbE 2-port)", "speed": "10GbE", "ports": 2},
    {"desc": "Broadcom BCM5719 (1GbE 4-port)", "speed": "1GbE", "ports": 4},
    {"desc": "Broadcom BCM57454 (10/25GbE 4-port)", "speed": "25GbE", "ports": 4},
    {"desc": "Broadcom BCM57504 (10/25GbE SFP28 4-port OCP)", "speed": "25GbE", "ports": 4},
]

DISK_SIZES_TB = {
    "hdd": [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
    "ssd": [0.24, 0.48, 0.96, 1.92, 3.84, 7.68, 15.36, 30.72],
    "nvme": [0.25, 0.48, 0.5, 0.96, 1, 1.92, 2, 3.84, 4, 7.68, 8, 15.36, 30.72],
}

RAM_SIZES_GB = [4, 8, 16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072]

SWITCHING = [
    {"make": "Ubiquiti", "model": "Edge Switch 10 X", "sku": "B8-U8", "rj45": "8x 10/100/1000 Mbps RJ45", "sfp": "2x 1 Gbps SFP"},
    {"make": "Ubiquiti", "model": "Edge Switch 16 XG", "sku": "B16-UB-10", "rj45": "4x 10G RJ45", "sfp": "12x SFP+"},
    {"make": "Ubiquiti", "model": "Edge Switch 48 Lite", "sku": "B48-UB", "rj45": "48x Gigabit RJ45", "sfp": "2x SFP+, 2x SFP"},
    {"make": "Cisco", "model": "CBS350-XF", "sku": "B12-CSC-10X", "rj45": "2x 10GbE copper/SFP+ combo", "sfp": "10x 10GbE SFP+"},
    {"make": "Cisco", "model": "C1300-12XT", "sku": "B12-CSC-10xc", "rj45": "2x 10GBase-T/SFP+", "sfp": "10x SFP+ Fixed"},
    {"make": "Dell", "model": "S5212F-ON", "sku": "B24-DL-25", "rj45": "N/A", "sfp": "12x 10/25GbE SFP28, 3x 100GbE QSFP28"},
    {"make": "Mellanox", "model": "MSN2010-CB2F", "sku": "B34-MX", "rj45": "N/A", "sfp": "18x 10/25GbE SFP28, 4x 40/100GbE QSFP28"},
    {"make": "HP", "model": "ProCurve 2930", "sku": "B24-HP", "rj45": "24x 10/100/1000Base-T", "sfp": "4x SFP+"},
    {"make": "Netgear", "model": "M4300-24XF", "sku": "B24-NG-10", "rj45": "2x 10GBase-T", "sfp": "24x 10GBase-X SFP+"},
]
