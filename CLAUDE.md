# CLAUDE.md

Local-only PDF redaction tool. Strips PII (SSNs, account numbers, emails,
addresses, MRNs, custom names, …) from PDFs before the user uploads them to
AI tools. macOS, Python 3.9+, PyMuPDF.

## Hard constraints — never violate these

- **Local only.** No network calls, no telemetry, no uploading documents
  anywhere. Do not add dependencies that phone home.
- **Redaction must be permanent.** Use PyMuPDF redaction annotations +
  `page.apply_redactions()`, which deletes the underlying text. Never
  "redact" by drawing rectangles over text.
- **Never redact financial data**: balances, holdings, transactions,
  tickers, prices, cost basis, gains/losses, fees, dividends, performance,
  or ordinary dates. Only dates adjacent to a DOB label are redacted.
  Numeric patterns must stay contextual (require a nearby label) for this
  reason.
- **Never fake success on scanned PDFs.** Image-only pages are OCR'd
  (PyMuPDF's built-in Tesseract; `find_tessdata()` locates language data)
  and the matching image regions blanked via
  `apply_redactions(images=PDF_REDACT_IMAGE_PIXELS)`. If OCR is unavailable
  or yields nothing, the page is reported as unredacted (whole-doc case:
  exit code 2 + delete the output file). Verification re-OCRs those pages
  in the output.
- **Names are never guessed.** They come only from `custom_terms` in
  `config/redact_config.yaml` — no NLP/NER. This is deliberate.
- **exclude_terms must apply everywhere.** The config's never-redact list
  is honored in the redaction pass AND in verification; adding a detection
  surface (links, ToC, …) without threading `exclude` through both breaks
  it.
- Contextual regex patterns must not cross line breaks (use `SEP` /
  `[ \t]*`, not `\s*`, between label and value) — otherwise a label at a
  line end captures the next line's text.

## Commands

```bash
./scripts/run.sh [preset] [--dry-run]   # batch: input/ -> output/ (cleans output/ first; sets up .venv + tesseract on first run)
./scripts/redact.sh file.pdf --preset financial    # single file
./scripts/redact.sh --list-categories
.venv/bin/python src/make_sample_pdf.py # regenerate fake-data samples into input/
```

There is no test suite; verify changes by regenerating the samples and
running `./scripts/run.sh financial`, then checking:
1. exit status 0 and "Verify : PASS" per file (the tool re-scans its own
   output; scan pages are re-OCR'd),
2. the reports in `output/*_report.txt`,
3. that financial figures are still present in the output text
   (`page.get_text()`),
4. the scanned sample's output renders with the PII regions blanked.

## Layout

- `src/redact.py` — everything: `CATEGORY_PATTERNS` (regex + optional
  validator + optional named `redact` group for contextual matches),
  `PRESETS`, detection, OCR, redaction, post-redaction verification, report.
- `src/make_sample_pdf.py` — writes fake-data test PDFs into `input/`
  (one searchable, one image-only for the OCR path).
- `scripts/redact.sh` — thin single-file wrapper running src/redact.py
  with `.venv`'s Python.
- `scripts/run.sh` — env setup + batch processing of `input/` to `output/`;
  empties `output/` (except .gitkeep) at the start of each run; classifies
  redact.py exit codes (0 ok / 2 unreadable pages / 3 verify failed).
- `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md` — keep in sync with behavior
  changes.
- `config/redact_config.yaml` — user's personal config: `preset`,
  `categories` (per-detector true/false switches over the preset; email,
  phone, ssn, drivers_license, passport, credit_card ship as `true`),
  `custom_terms`, `exclude_terms`, `options` (ocr / redact_barcodes),
  `custom_patterns`; loaded by default on every run (`--config` overrides;
  missing explicit config = hard error). Gitignored and auto-created by
  run.sh from `config/redact_config.example.yaml` — edit the example for
  template changes; never commit or echo the personal copy's contents.
  All settings are documented in `docs/CONFIGURATION.md` — keep it in sync.
- README is deliberately simple/non-technical (drop in input/, run.sh,
  collect from output/, edit config) — technical detail belongs in docs/.
- `input/`, `output/` — user documents; contents are gitignored and must
  stay that way. All `*.pdf` and `*_report.txt` are gitignored repo-wide.
- pyzbar/Pillow are optional (QR/barcode redaction); code must keep working
  without them.
