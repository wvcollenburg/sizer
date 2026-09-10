"""BOM checker package (docs/bom-checker-plan.md).

Sub-modules are pure Python with no Flask dependency unless stated:
  normalize   - NormalizedBOM dataclasses + JSON round-trip
  rules       - deterministic HCL/technical validation (port of SC//Design validator)
  hcl_scrape  - hcl.scalecomputing.com page parsers + fetcher
  hcl_sync    - scrape snapshot -> pending-change diff -> approval apply (ORM)
  parsers     - deterministic file parsers (Lenovo DCSC, Dell, strict template)
  fit         - BOM vs sizing capacity comparison
  ai_prefill  - optional, config-gated template pre-fill
"""
