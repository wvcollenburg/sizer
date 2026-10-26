"""BOM checker package (docs/bom-checker-plan.md).

Sub-modules are pure Python with no Flask dependency unless stated:
  agent_ingest   - Tier 3 of the ingestion ladder: the Claude agent reads what no parser can
  agent_worker   - background worker for BOM agent jobs (bom_models.BomAgentJob)
  check          - run one BOM check: technical validation + enrichment + optional sizing fit
  enrich         - second pass over a validation using what the scraped catalog knows
  facts          - what a BOM file says about the hardware, as a small stable record
  fit            - sizing fit: does a third-party BOM carry the workload of a sizing?
  hcl_data       - catalog -> rules.HclData adapter and small lookup helpers
  hcl_scrape     - hcl.scalecomputing.com page parsers + fetcher
  hcl_sync       - scrape snapshot <-> catalog diff and the approval queue (ORM)
  normalize      - NormalizedBOM: the one shape every ingestion path must produce
  part_notes     - apply reviewers' part notes to a BOM check's findings
  pdf_certainty  - how sure are we that a PDF parse is the whole, honest BOM? (0-100)
  sheet_certainty - the same score for a spreadsheet read by column shape (parsers/sheet_guess)
  pdf_doc        - a PDF, read safely, as the words a person would see
  pdf_extract    - PDF -> positioned words, rules and safety signals, in a separate process
  platform_match - map a BOM config to the HC-Ready platform(s) it is built on
  preview        - pre-publication accepts: parts verified but not yet published on the HCL
  rules          - deterministic BOM validation, a 1:1 port of SC//Design's validator.ts
  text           - small text helpers (whitespace/alnum keys, text decoding) the modules share
  parsers        - deterministic file parsers (Lenovo DCSC, Dell, Supermicro, PDFs, strict template)
                   and the best-effort column-shape reader for spreadsheets no parser knows
"""
