"""Authoritative CPU spec dataset for the catalog (feature/add-real-cpu-details).

Every entry was verified against Intel ARK / AMD.com / official Intel SKU-stack
launch decks, NOT parsed from the existing catalog descriptions (those were wrong
in several places). Intel publishes THREE clocks per server SKU:

    base_ghz            guaranteed all-core floor
    all_core_turbo_ghz  sustained freq with every core loaded  <- SIZING BASIS
    max_turbo_ghz       single-core peak (marketing headline)

All three are kept: base/max for presentation (these are the ARK-matching numbers
to show a customer), all-core for sensible sizing (the consolidated-VM case). The
sizing clock is all_core_turbo_ghz, falling back to base_ghz where a part has no
published all-core figure -- see sizing_ghz().

Hybrid parts (Core / Core Ultra) also carry ecore_base_ghz / ecore_turbo_ghz.

Performance index for sizing fidelity (GHz x cores misses generational IPC):
    specrate_int        SPECrate2017_int_base per socket (server parts; None for
                        desktop). The sizing throughput metric.
    passmark_cpu_mark   PassMark CPU Mark (multi) -- used for desktop parts and as
                        the SPECrate<->PassMark anchor. None where PassMark lacks a
                        single-CPU page (4516Y+, 6530P).
    passmark_single     PassMark Single-Thread Rating (per-core/IPC).
perf_index() returns a unified throughput figure on the SPECrate scale (desktop
PassMark is converted via the anchor fit).

All-core-turbo provenance:
  Cascade Lake (Gen2)   - Wikipedia "List of Intel Xeon (Cascade Lake-based)"
  Ice Lake (Gen3)       - Intel 3rd Gen Xeon Scalable SKU-stack deck
  Sapphire Rapids (Gen4)- Intel 4th Gen Xeon Scalable SKU-stack deck
  Emerald Rapids (Gen5) - Intel 5th Gen Xeon Scalable SKU-stack deck
  Granite Rapids (Xeon6)- Intel Xeon 6 P-core SKU-stack deck
  EPYC 9354             - AMD.com (all-core boost 3.75)
  Xeon E (all 11)       - Intel datasheets 662318 / 338014 + KB 000096435
                          (per-active-core turbo tables)
  Core 10th/11th-gen U, i9-13900H - Notebookcheck (Intel stopped publishing
                          client all-core turbo). NOTE: these mobile figures are
                          cooling-unconstrained PEAKS, not sustained at low TDP
                          (e.g. i7-10710U peaks 3.9 but sustains ~1.9 at 15W) --
                          so they read optimistically as a sizing clock for thin
                          edge devices.
i7-1370P and the Core Ultra 2xxH parts have no documented all-core turbo
(all_core_turbo_ghz=None) and therefore size on base_ghz.

`cpu_model_key(desc)` extracts the canonical SKU key from a (messy) catalog
description so existing rows can be matched and back-filled. Returns None for
non-CPU placeholders like "16 Threads".
"""

import re

# key -> spec. base/all_core_turbo/max_turbo are P-core clocks; ecore_* are the
# E-core base/max for hybrids (None otherwise). cores/threads are ARK totals.
# all_core_turbo_ghz is None where no authoritative all-core figure was found.
CPU_SPECS = {
    # ── Intel Xeon Scalable — Gen 2 (Cascade Lake) ──────────────────────────
    "3204":   dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Bronze 3204", cores=6,  threads=6,  p_cores=6,  e_cores=0, base_ghz=1.9, all_core_turbo_ghz=1.9, max_turbo_ghz=1.9, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=20.3, passmark_cpu_mark=4745, passmark_single=1126, source="https://ark.intel.com/content/www/us/en/ark/products/193381"),
    "4208":   dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Silver 4208", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=2.1, all_core_turbo_ghz=2.5, max_turbo_ghz=3.2, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=41.7, passmark_cpu_mark=10921, passmark_single=1664, source="https://ark.intel.com/content/www/us/en/ark/products/193390"),
    "4210R":  dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Silver 4210R", cores=10, threads=20, p_cores=10, e_cores=0, base_ghz=2.4, all_core_turbo_ghz=2.9, max_turbo_ghz=3.2, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=57.0, passmark_cpu_mark=14976, passmark_single=1803, source="https://ark.intel.com/content/www/us/en/ark/products/197098"),
    "4215R":  dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Silver 4215R", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=3.2, all_core_turbo_ghz=3.6, max_turbo_ghz=4.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=54.5, passmark_cpu_mark=14939, passmark_single=2171, source="https://ark.intel.com/content/www/us/en/ark/products/199349"),
    "5218R":  dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Gold 5218R", cores=20, threads=40, p_cores=20, e_cores=0, base_ghz=2.1, all_core_turbo_ghz=2.8, max_turbo_ghz=4.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=112.0, passmark_cpu_mark=25076, passmark_single=2191, source="https://ark.intel.com/content/www/us/en/ark/products/199342"),
    "6226":   dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Gold 6226", cores=12, threads=24, p_cores=12, e_cores=0, base_ghz=2.7, all_core_turbo_ghz=3.5, max_turbo_ghz=3.7, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=82.0, passmark_cpu_mark=20496, passmark_single=2105, source="https://ark.intel.com/content/www/us/en/ark/products/193957"),
    "6226R":  dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Gold 6226R", cores=16, threads=32, p_cores=16, e_cores=0, base_ghz=2.9, all_core_turbo_ghz=3.6, max_turbo_ghz=3.9, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=108.5, passmark_cpu_mark=26386, passmark_single=2256, source="https://ark.intel.com/content/www/us/en/ark/products/199347"),
    "6230R":  dict(make="Intel", family="Xeon Scalable", generation="Gen 2 (Cascade Lake)", model="Xeon Gold 6230R", cores=26, threads=52, p_cores=26, e_cores=0, base_ghz=2.1, all_core_turbo_ghz=2.3, max_turbo_ghz=4.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=142.0, passmark_cpu_mark=32591, passmark_single=2169, source="https://ark.intel.com/content/www/us/en/ark/products/199346"),
    # ── Intel Xeon Scalable — Gen 3 (Ice Lake) ──────────────────────────────
    "5315Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 3 (Ice Lake)", model="Xeon Gold 5315Y", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=3.2, all_core_turbo_ghz=3.5, max_turbo_ghz=3.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=69.0, passmark_cpu_mark=20477, passmark_single=2442, source="https://ark.intel.com/content/www/us/en/ark/products/215286"),
    "5317":   dict(make="Intel", family="Xeon Scalable", generation="Gen 3 (Ice Lake)", model="Xeon Gold 5317", cores=12, threads=24, p_cores=12, e_cores=0, base_ghz=3.0, all_core_turbo_ghz=3.4, max_turbo_ghz=3.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=103.5, passmark_cpu_mark=27293, passmark_single=2327, source="https://ark.intel.com/content/www/us/en/ark/products/215272"),
    "6326":   dict(make="Intel", family="Xeon Scalable", generation="Gen 3 (Ice Lake)", model="Xeon Gold 6326", cores=16, threads=32, p_cores=16, e_cores=0, base_ghz=2.9, all_core_turbo_ghz=3.3, max_turbo_ghz=3.5, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=131.0, passmark_cpu_mark=32738, passmark_single=2257, source="https://ark.intel.com/content/www/us/en/ark/products/215274"),
    "6336Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 3 (Ice Lake)", model="Xeon Gold 6336Y", cores=24, threads=48, p_cores=24, e_cores=0, base_ghz=2.4, all_core_turbo_ghz=3.0, max_turbo_ghz=3.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=178.5, passmark_cpu_mark=45517, passmark_single=2522, source="https://ark.intel.com/content/www/us/en/ark/products/215280"),
    "6338N":  dict(make="Intel", family="Xeon Scalable", generation="Gen 3 (Ice Lake)", model="Xeon Gold 6338N", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=2.2, all_core_turbo_ghz=2.7, max_turbo_ghz=3.5, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=202.0, passmark_cpu_mark=42086, passmark_single=2066, source="https://ark.intel.com/content/www/us/en/ark/products/212633"),
    # ── Intel Xeon Scalable — Gen 4 (Sapphire Rapids) ───────────────────────
    "4410Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 4 (Sapphire Rapids)", model="Xeon Silver 4410Y", cores=12, threads=24, p_cores=12, e_cores=0, base_ghz=2.0, all_core_turbo_ghz=2.8, max_turbo_ghz=3.9, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=108.5, passmark_cpu_mark=25162, passmark_single=2476, source="https://www.intel.com/content/www/us/en/products/sku/232376"),
    "5415+":  dict(make="Intel", family="Xeon Scalable", generation="Gen 4 (Sapphire Rapids)", model="Xeon Gold 5415+", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=2.9, all_core_turbo_ghz=3.6, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=87.5, passmark_cpu_mark=24906, passmark_single=3128, source="https://www.intel.com/content/www/us/en/products/sku/232373"),
    "5418Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 4 (Sapphire Rapids)", model="Xeon Gold 5418Y", cores=24, threads=48, p_cores=24, e_cores=0, base_ghz=2.0, all_core_turbo_ghz=2.8, max_turbo_ghz=3.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=206.0, passmark_cpu_mark=45660, passmark_single=2747, source="https://www.intel.com/content/www/us/en/products/sku/232379"),
    "6426Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 4 (Sapphire Rapids)", model="Xeon Gold 6426Y", cores=16, threads=32, p_cores=16, e_cores=0, base_ghz=2.5, all_core_turbo_ghz=3.3, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=164.0, passmark_cpu_mark=37944, passmark_single=2769, source="https://www.intel.com/content/www/us/en/products/sku/232377"),
    "6438N":  dict(make="Intel", family="Xeon Scalable", generation="Gen 4 (Sapphire Rapids)", model="Xeon Gold 6438N", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=2.0, all_core_turbo_ghz=2.8, max_turbo_ghz=3.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=279.5, passmark_cpu_mark=52789, passmark_single=2315, source="https://www.intel.com/content/www/us/en/products/sku/232397"),
    # ── Intel Xeon Scalable — Gen 5 (Emerald Rapids) ────────────────────────
    "4516Y+": dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Silver 4516Y+", cores=24, threads=48, p_cores=24, e_cores=0, base_ghz=2.2, all_core_turbo_ghz=2.9, max_turbo_ghz=3.7, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=219.0, passmark_cpu_mark=None, passmark_single=None, source="https://www.intel.com/content/www/us/en/products/sku/237556"),
    "5515+":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 5515+", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=3.2, all_core_turbo_ghz=3.6, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=88.0, passmark_cpu_mark=26359, passmark_single=3013, source="https://www.intel.com/content/www/us/en/products/sku/237562"),
    "5520+":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 5520+", cores=28, threads=56, p_cores=28, e_cores=0, base_ghz=2.2, all_core_turbo_ghz=3.0, max_turbo_ghz=4.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=250.5, passmark_cpu_mark=61227, passmark_single=3216, source="https://www.intel.com/content/www/us/en/products/sku/237561"),
    "6526Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 6526Y", cores=16, threads=32, p_cores=16, e_cores=0, base_ghz=2.8, all_core_turbo_ghz=3.5, max_turbo_ghz=3.9, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=169.5, passmark_cpu_mark=42195, passmark_single=2972, source="https://www.intel.com/content/www/us/en/products/sku/237560"),
    "6538N":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 6538N", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=2.1, all_core_turbo_ghz=2.9, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=285.0, passmark_cpu_mark=44895, passmark_single=1725, source="https://www.intel.com/content/www/us/en/products/sku/237568"),
    "6542Y":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 6542Y", cores=24, threads=48, p_cores=24, e_cores=0, base_ghz=2.9, all_core_turbo_ghz=3.6, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=256.0, passmark_cpu_mark=60144, passmark_single=3087, source="https://www.intel.com/content/www/us/en/products/sku/237559"),
    "6548N":  dict(make="Intel", family="Xeon Scalable", generation="Gen 5 (Emerald Rapids)", model="Xeon Gold 6548N", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=2.8, all_core_turbo_ghz=3.5, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=319.5, passmark_cpu_mark=64783, passmark_single=2844, source="https://www.intel.com/content/www/us/en/products/sku/237567"),
    # ── Intel Xeon 6 (Granite Rapids) — catalog mislabels these "Gold" ───────
    "6505P":  dict(make="Intel", family="Xeon 6", generation="Xeon 6 (Granite Rapids)", model="Xeon 6505P", cores=12, threads=24, p_cores=12, e_cores=0, base_ghz=2.2, all_core_turbo_ghz=3.9, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=142.5, passmark_cpu_mark=38456, passmark_single=3187, source="https://www.intel.com/content/www/us/en/products/sku/242667"),
    "6507P":  dict(make="Intel", family="Xeon 6", generation="Xeon 6 (Granite Rapids)", model="Xeon 6507P", cores=8,  threads=16, p_cores=8,  e_cores=0, base_ghz=3.5, all_core_turbo_ghz=4.3, max_turbo_ghz=4.3, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=105.0, passmark_cpu_mark=31233, passmark_single=3643, source="https://www.intel.com/content/www/us/en/products/sku/242668"),
    "6517P":  dict(make="Intel", family="Xeon 6", generation="Xeon 6 (Granite Rapids)", model="Xeon 6517P", cores=16, threads=32, p_cores=16, e_cores=0, base_ghz=3.2, all_core_turbo_ghz=4.0, max_turbo_ghz=4.2, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=194.0, passmark_cpu_mark=49768, passmark_single=3307, source="https://www.intel.com/content/www/us/en/products/sku/242665"),
    "6520P":  dict(make="Intel", family="Xeon 6", generation="Xeon 6 (Granite Rapids)", model="Xeon 6520P", cores=24, threads=48, p_cores=24, e_cores=0, base_ghz=2.4, all_core_turbo_ghz=3.4, max_turbo_ghz=4.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=259.5, passmark_cpu_mark=62936, passmark_single=3356, source="https://www.intel.com/content/www/us/en/products/sku/242640"),
    "6530P":  dict(make="Intel", family="Xeon 6", generation="Xeon 6 (Granite Rapids)", model="Xeon 6530P", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=2.3, all_core_turbo_ghz=3.7, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=353.0, passmark_cpu_mark=None, passmark_single=None, source="https://www.intel.com/content/www/us/en/products/sku/242636"),
    # ── Intel Xeon E (all-core turbo: only E-2234 / E-2236 found authoritatively) ─
    "E-2224": dict(make="Intel", family="Xeon E", generation="E-2200 (Coffee Lake)", model="Xeon E-2224", cores=4, threads=4,  p_cores=4, e_cores=0, base_ghz=3.4, all_core_turbo_ghz=4.2, max_turbo_ghz=4.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=27.4, passmark_cpu_mark=7169, passmark_single=2542, source="https://www.intel.com/content/www/us/en/products/sku/191036"),
    "E-2234": dict(make="Intel", family="Xeon E", generation="E-2200 (Coffee Lake)", model="Xeon E-2234", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=3.6, all_core_turbo_ghz=4.5, max_turbo_ghz=4.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=35.1, passmark_cpu_mark=9659, passmark_single=2795, source="https://www.intel.com/content/www/us/en/products/sku/191039"),
    "E-2236": dict(make="Intel", family="Xeon E", generation="E-2200 (Coffee Lake)", model="Xeon E-2236", cores=6, threads=12, p_cores=6, e_cores=0, base_ghz=3.4, all_core_turbo_ghz=4.5, max_turbo_ghz=4.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=42.8, passmark_cpu_mark=13775, passmark_single=2788, source="https://www.intel.com/content/www/us/en/products/sku/191040"),
    "E-2324G":dict(make="Intel", family="Xeon E", generation="E-2300 (Rocket Lake)", model="Xeon E-2324G", cores=4, threads=4,  p_cores=4, e_cores=0, base_ghz=3.1, all_core_turbo_ghz=4.5, max_turbo_ghz=4.6, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=33.0, passmark_cpu_mark=10339, passmark_single=3086, source="https://www.intel.com/content/www/us/en/products/sku/212255"),
    "E-2334": dict(make="Intel", family="Xeon E", generation="E-2300 (Rocket Lake)", model="Xeon E-2334", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=3.4, all_core_turbo_ghz=4.6, max_turbo_ghz=4.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=39.6, passmark_cpu_mark=12157, passmark_single=3037, source="https://www.intel.com/content/www/us/en/products/sku/212258"),
    "E-2386G":dict(make="Intel", family="Xeon E", generation="E-2300 (Rocket Lake)", model="Xeon E-2386G", cores=6, threads=12, p_cores=6, e_cores=0, base_ghz=3.5, all_core_turbo_ghz=4.7, max_turbo_ghz=5.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=57.3, passmark_cpu_mark=19521, passmark_single=3398, source="https://www.intel.com/content/www/us/en/products/sku/214806"),
    "E-2388G":dict(make="Intel", family="Xeon E", generation="E-2300 (Rocket Lake)", model="Xeon E-2388G", cores=8, threads=16, p_cores=8, e_cores=0, base_ghz=3.2, all_core_turbo_ghz=4.6, max_turbo_ghz=5.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=68.9, passmark_cpu_mark=23458, passmark_single=3399, source="https://www.intel.com/content/www/us/en/products/sku/214805"),
    "E-2414": dict(make="Intel", family="Xeon E", generation="E-2400 (Raptor Lake)", model="Xeon E-2414", cores=4, threads=4,  p_cores=4, e_cores=0, base_ghz=2.6, all_core_turbo_ghz=4.3, max_turbo_ghz=4.5, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=39.7, passmark_cpu_mark=11820, passmark_single=3552, source="https://www.intel.com/content/www/us/en/products/sku/236193"),
    "E-2434": dict(make="Intel", family="Xeon E", generation="E-2400 (Raptor Lake)", model="Xeon E-2434", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=3.4, all_core_turbo_ghz=4.6, max_turbo_ghz=5.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=50.3, passmark_cpu_mark=15228, passmark_single=3912, source="https://www.intel.com/content/www/us/en/products/sku/236192"),
    "E-2436": dict(make="Intel", family="Xeon E", generation="E-2400 (Raptor Lake)", model="Xeon E-2436", cores=6, threads=12, p_cores=6, e_cores=0, base_ghz=2.9, all_core_turbo_ghz=4.4, max_turbo_ghz=5.0, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=69.5, passmark_cpu_mark=21635, passmark_single=3601, source="https://www.intel.com/content/www/us/en/products/sku/236190"),
    "E-2468": dict(make="Intel", family="Xeon E", generation="E-2400 (Raptor Lake)", model="Xeon E-2468", cores=8, threads=16, p_cores=8, e_cores=0, base_ghz=2.6, all_core_turbo_ghz=4.4, max_turbo_ghz=5.2, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=84.4, passmark_cpu_mark=26652, passmark_single=4003, source="https://www.intel.com/content/www/us/en/products/sku/236184"),
    # ── Intel Core (edge; Intel publishes no all-core turbo for client parts) ─
    "i3-10110U": dict(make="Intel", family="Core", generation="10th Gen (Comet Lake)", model="Core i3-10110U", cores=2, threads=4,  p_cores=2, e_cores=0, base_ghz=2.1, all_core_turbo_ghz=3.7, max_turbo_ghz=4.1, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=None, passmark_cpu_mark=3818, passmark_single=2119, source="https://ark.intel.com/content/www/us/en/ark/products/196451"),
    "i5-10210U": dict(make="Intel", family="Core", generation="10th Gen (Comet Lake)", model="Core i5-10210U", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=1.6, all_core_turbo_ghz=3.9, max_turbo_ghz=4.2, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=None, passmark_cpu_mark=6054, passmark_single=2107, source="https://ark.intel.com/content/www/us/en/ark/products/195436"),
    "i7-10710U": dict(make="Intel", family="Core", generation="10th Gen (Comet Lake)", model="Core i7-10710U", cores=6, threads=12, p_cores=6, e_cores=0, base_ghz=1.1, all_core_turbo_ghz=3.9, max_turbo_ghz=4.7, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=None, passmark_cpu_mark=9163, passmark_single=2265, source="https://ark.intel.com/content/www/us/en/ark/products/196448"),
    "i5-1145G7": dict(make="Intel", family="Core", generation="11th Gen (Tiger Lake)", model="Core i5-1145G7", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=2.6, all_core_turbo_ghz=3.8, max_turbo_ghz=4.4, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=None, passmark_cpu_mark=9180, passmark_single=2659, source="https://ark.intel.com/content/www/us/en/ark/products/208660"),
    "i7-1185G7": dict(make="Intel", family="Core", generation="11th Gen (Tiger Lake)", model="Core i7-1185G7", cores=4, threads=8,  p_cores=4, e_cores=0, base_ghz=3.0, all_core_turbo_ghz=4.3, max_turbo_ghz=4.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=None, passmark_cpu_mark=9921, passmark_single=2754, source="https://ark.intel.com/content/www/us/en/ark/products/208664"),
    "i7-1370P":  dict(make="Intel", family="Core", generation="13th Gen (Raptor Lake)", model="Core i7-1370P", cores=14, threads=20, p_cores=6, e_cores=8, base_ghz=1.9, all_core_turbo_ghz=None, max_turbo_ghz=5.2, ecore_base_ghz=1.4, ecore_turbo_ghz=3.9, specrate_int=None, passmark_cpu_mark=19686, passmark_single=3474, source="https://ark.intel.com/content/www/us/en/ark/products/232146"),
    "i9-13900H": dict(make="Intel", family="Core", generation="13th Gen (Raptor Lake)", model="Core i9-13900H", cores=14, threads=20, p_cores=6, e_cores=8, base_ghz=2.6, all_core_turbo_ghz=4.9, max_turbo_ghz=5.4, ecore_base_ghz=1.9, ecore_turbo_ghz=4.1, specrate_int=None, passmark_cpu_mark=27135, passmark_single=3704, source="https://ark.intel.com/content/www/us/en/ark/products/232135"),
    # ── Intel Core Ultra (Series 2, Arrow Lake-H; no published all-core turbo) ─
    "Ultra 5 225H": dict(make="Intel", family="Core Ultra", generation="Series 2 (Arrow Lake)", model="Core Ultra 5 225H", cores=14, threads=14, p_cores=4, e_cores=10, base_ghz=1.7, all_core_turbo_ghz=None, max_turbo_ghz=4.9, ecore_base_ghz=1.3, ecore_turbo_ghz=4.3, specrate_int=None, passmark_cpu_mark=28295, passmark_single=4256, source="https://www.intel.com/content/www/us/en/products/sku/241749"),
    "Ultra 5 235H": dict(make="Intel", family="Core Ultra", generation="Series 2 (Arrow Lake)", model="Core Ultra 5 235H", cores=14, threads=14, p_cores=4, e_cores=10, base_ghz=2.4, all_core_turbo_ghz=None, max_turbo_ghz=5.0, ecore_base_ghz=1.8, ecore_turbo_ghz=4.4, specrate_int=None, passmark_cpu_mark=30023, passmark_single=4338, source="https://www.intel.com/content/www/us/en/products/sku/241748"),
    "Ultra 7 255H": dict(make="Intel", family="Core Ultra", generation="Series 2 (Arrow Lake)", model="Core Ultra 7 255H", cores=16, threads=16, p_cores=6, e_cores=10, base_ghz=2.0, all_core_turbo_ghz=None, max_turbo_ghz=5.1, ecore_base_ghz=1.5, ecore_turbo_ghz=4.4, specrate_int=None, passmark_cpu_mark=30725, passmark_single=4313, source="https://www.intel.com/content/www/us/en/products/sku/241751"),
    "Ultra 7 265H": dict(make="Intel", family="Core Ultra", generation="Series 2 (Arrow Lake)", model="Core Ultra 7 265H", cores=16, threads=16, p_cores=6, e_cores=10, base_ghz=2.2, all_core_turbo_ghz=None, max_turbo_ghz=5.3, ecore_base_ghz=1.7, ecore_turbo_ghz=4.5, specrate_int=None, passmark_cpu_mark=34162, passmark_single=4347, source="https://www.intel.com/content/www/us/en/products/sku/241750"),
    # ── AMD EPYC ────────────────────────────────────────────────────────────
    "EPYC 9354": dict(make="AMD", family="EPYC", generation="EPYC 9004 (Genoa, Zen 4)", model="EPYC 9354", cores=32, threads=64, p_cores=32, e_cores=0, base_ghz=3.25, all_core_turbo_ghz=3.75, max_turbo_ghz=3.8, ecore_base_ghz=None, ecore_turbo_ghz=None, specrate_int=371.0, passmark_cpu_mark=73240, passmark_single=2766, source="https://www.amd.com/en/products/processors/server/epyc/4th-generation-9004-and-8004-series/amd-epyc-9354.html"),
}


# Client/mobile families whose all-core turbo is a cooling-unconstrained PEAK
# they won't hold continuously in a thin edge device. These size on the
# guaranteed BASE clock instead; their all-core/max figures are kept for
# presentation only. (Server parts run in proper datacenter cooling at their
# rated power, so their published all-core turbo IS a sustained figure.)
_BASE_CLOCK_FAMILIES = {"Core", "Core Ultra"}


def sizing_ghz(spec):
    """The clock the recommendation engine sizes on. Server parts use all-core
    turbo (sustained, datacenter-cooled); Core / Core Ultra client parts use the
    guaranteed base clock (their all-core is an unsustainable burst on edge
    hardware). Falls back to base where no all-core figure exists."""
    if spec.get("family") in _BASE_CLOCK_FAMILIES:
        return spec["base_ghz"]
    return spec.get("all_core_turbo_ghz") or spec["base_ghz"]


# PassMark CPU Mark -> SPECrate2017_int_base (per-socket) conversion, from a
# 40-CPU anchor fit (server parts with both scores; correlation 0.97). Lets
# desktop CPUs (PassMark-only) land on the SPECrate scale. Approximate (~20%
# mean error); PassMark is burst-oriented so mobile parts read optimistically.
PASSMARK_CPU_MARK_TO_SPECRATE = 0.00386


def perf_index(spec):
    """Unified throughput index on the SPECrate2017_int (per-socket) scale.
    Server parts use native SPECrate; desktop parts convert from PassMark CPU
    Mark. None if neither score is available."""
    if spec.get("specrate_int") is not None:
        return spec["specrate_int"]
    if spec.get("passmark_cpu_mark") is not None:
        return round(spec["passmark_cpu_mark"] * PASSMARK_CPU_MARK_TO_SPECRATE, 1)
    return None


# Ordered extraction rules. First match wins; returns the CPU_SPECS key.
_TIER = r"(?:Bronze|Silver|Gold|Platinum)"


def cpu_model_key(desc):
    """Pull the canonical SKU key out of a catalog description, or None.

    Handles the inconsistent real-world strings: "Gold 6542Y 24C/48T 3.6GHz",
    "Intel Xeon E-2468 @ 4.4 GHz", "AMD EPYC 9354 32-Core", "Xeon 6517P ...",
    "Gold 6517P ..." (mislabeled Xeon 6 -- same key), "i7-1185G7 ...",
    "Ultra 7 265H ...". Non-CPU placeholders ("16 Threads") -> None.
    """
    d = desc.strip()
    m = re.search(r"EPYC\s+(\d{4}[A-Z]*)", d, re.I)
    if m:
        return f"EPYC {m.group(1).upper()}"
    m = re.search(r"\bE-(\d{4}[A-Z]*)", d, re.I)
    if m:
        return f"E-{m.group(1).upper()}"
    m = re.search(r"Ultra\s+(\d)\s+(\d{3}[A-Z]+)", d, re.I)
    if m:
        return f"Ultra {m.group(1)} {m.group(2).upper()}"
    m = re.search(r"\b(i[3579]-\d{3,5}[A-Z]*\d*[A-Z]*)", d, re.I)
    if m:
        return m.group(1)
    # Xeon Scalable (tiered) and Xeon 6 (Xeon NNNNP) both reduce to the SKU.
    m = re.search(_TIER + r"\s+(\d{4}[A-Z+]*)", d, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"Xeon\s+(\d{4}[A-Z+]*)", d, re.I)
    if m:
        return m.group(1).upper()
    return None
