# CLAUDE.md

Local-only document redaction tool. Strips PII (SSNs, account numbers,
emails, addresses, MRNs, custom names, …) from PDFs, Office docs, images,
spreadsheets, CSV, and text before the user uploads them to AI tools.
macOS, Homebrew Python 3.13 (run.sh bootstraps it; system 3.9 lacks
wheels for pinned deps), PyMuPDF.

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
- Pattern boundary guards must reject only digit CONTINUATIONS, never
  sentence punctuation: `(?![\d.\-])` silently missed every phone followed
  by a period ("…9999.") — a real bug found by the test suite. Use
  `(?!\d)(?![.\-]\d)` style guards.
- `find_matches_in_text` scans the raw text AND a whitespace-normalized
  copy (values wrapped across line breaks evade single-whitespace
  separators). Keep both scans when touching it.
- Tabular handlers (csv, xlsx) scan each data cell bare AND as
  `"column header: value"` (header-as-context) — otherwise labeled
  detectors never fire in tables; and they must use tabular_categories()
  (drops the bare digit-run pattern) so numeric financial columns are
  never mass-redacted.
- Every new handler MUST implement post-redaction verification
  (verify_file / re-scan of the written output). No verification = does
  not ship. Run `./scripts/test.sh` after any detection or handler change.
- `route_input()` classifies by magic bytes before extension for a
  reason: RTF/EML are plain ASCII and would otherwise fall into the
  generic text-sniff fallback and get corrupted (control codes /
  boundaries treated as plain content) — a real bug found while adding
  the legacy Office/email handlers. New magic-byte-ambiguous formats
  (anything ASCII, or sharing OLE2/zip signatures with other formats)
  need the same explicit interception, not the fallback.
- Reports live in a `logs/` subfolder next to the output file, computed
  centrally by `default_report_path()` — every write path (`write_report`,
  `write_native_report`, `combine_outputs.py`) must call it rather than
  building the path inline, or a new handler will drop its report
  straight into `output/` and break the convention. The path is derived
  from the output's FULL filename (extension included), never `.stem` —
  `.stem` strips only the last extension, so two same-stem
  different-extension outputs (e.g. `foo_redacted.txt` and
  `foo_redacted.yaml`, an ordinary same-directory batch) would silently
  collapse to one report, each overwriting the other's audit trail. A
  real bug found while building `combine_outputs.py`, which trusts each
  file's report to decide whether it's safe to merge. An explicit
  `--report PATH` is honored exactly as given, no forced subfolder.
- `office_converter: msoffice` (AppleScript automation of installed MS
  Office) is a documented dead end, not an unimplemented feature — live-
  tested against this Mac's real Word install and confirmed broken (no
  working AppleScript PDF export in this build). Selecting it must keep
  erroring clearly; do not "fix" it by guessing new AppleScript without
  live-testing against a real running Office app first.

## Commands

```bash
./scripts/run.sh [preset] [--dry-run]   # batch: input/ -> output/, ANY supported format (cleans output/ first; bootstraps python@3.13 + deps + tesseract)
./scripts/redact.sh file.docx --preset financial   # single file, any format
./scripts/redact.sh --list-categories
./scripts/redact.sh --check-config      # audit what the config resolves to
./scripts/test.sh                       # REQUIRED gate: planted-PII regression suite, all formats
.venv/bin/python src/make_sample_pdf.py # regenerate fake-data samples into input/
```

Verification for changes: `./scripts/test.sh` (generates fixtures for
every format, redacts, asserts planted PII gone + $12,345.67 survives),
plus `./scripts/run.sh` over input/ expecting all PASS. Exit codes:
0 ok / 2 unreadable pages / 3 verify failed / 4 unsupported format /
5 encrypted, could not open.

## Layout

- `src/redact.py` — engine: `CATEGORY_PATTERNS` (regex + optional
  validator + optional named `redact` group for contextual matches),
  `PRESETS`, PDF detection/OCR/redaction/verification/report, the format
  **router** (`route_input`, magic-bytes-first dispatch to handlers), the
  convert/native execution flows (`run_convert_flow`/`run_native_flow`),
  config loading/linting, and the CLI.
- `src/handlers/` — one module per format family; see
  `docs/plans/handler-spec.md` for the binding contract (Kind A convert
  handlers expose `to_pdf`/`write_back`; Kind B native handlers expose
  `redact_file`/`verify_file`, both mandatory). Every handler MUST
  implement post-redaction verification — no verification, does not ship.
  `pdf_render.py` is the shared text-to-PDF renderer (`PdfFlow`,
  `html_to_text()`, `render_to_pdf_bytes()`) — reuse it rather than
  reimplementing text-flow/wrap logic in a new handler.
  `vision_helper.py` wraps Apple Vision (face/handwriting) — lazy-import
  Vision/Quartz inside functions, never at module top, since the three
  options that use it default off.
- `src/make_sample_pdf.py` — writes fake-data test PDFs into `input/`
  (one searchable, one image-only for the OCR path).
- `tests/make_*_fixtures.py` + `tests/check_outputs.py` — planted-PII
  fixture generators (one per format family) and the assertion that
  checks every redacted output for leaked PII / preserved financial data.
- `scripts/redact.sh` — thin single-file wrapper running src/redact.py
  with `.venv`'s Python.
- `scripts/run.sh` — bootstraps Homebrew python@3.13 (rebuilds `.venv` on
  version mismatch), deps, Tesseract, personal config; prompts (once,
  cached in `config/.libreoffice_declined`) to install LibreOffice via
  Homebrew if the batch needs it and it's absent; batch-processes every
  file in `input/` to `output/` (any supported format, not just PDF);
  empties `output/` (except .gitkeep) at the start of each run;
  classifies redact.py exit codes (0 ok / 2 unreadable / 3 verify failed /
  4 unsupported / 5 encrypted); `--combine` runs `src/combine_outputs.py`
  over `output/` afterward.
- `scripts/test.sh` — required regression gate; run after any change to
  detection patterns or handlers.
- `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md` —
  keep in sync with behavior changes.
- `docs/plans/` — all plan documents live here.
  `format-support-plan.md`: multi-format support + Python 3.13 upgrade,
  EXECUTED 2026-07-09 (§8 has the execution record, deviations, and bugs
  the test suite found); `handler-spec.md`: the handler contract —
  consult before touching or adding a handler; `expansion-plan.md`:
  legacy Office/RTF, email, epub, passwords, Apple Vision handwriting/
  faces, and everything-to-PDF + combine-into-one-PDF — EXECUTED
  2026-07-10 (§8 has the execution record; MS Office AppleScript
  automation is a documented dead end, not missing; `.msg`/`redact_faces`
  are implemented but fixture-unverified — see §8 before trusting them).
- `custom_terms`/`exclude_terms` accept flat lists OR grouped mappings —
  `flatten_terms()` handles both. This was a real silent-miss bug (group
  labels matched instead of names); never regress it.
- `config/redact_config.yaml` — user's personal config: `preset`,
  `categories` (per-detector true/false switches over the preset; email,
  phone, ssn, drivers_license, passport, credit_card ship as `true`),
  `custom_terms`, `exclude_terms`, `options` (ocr / redact_barcodes /
  office_converter / handwriting_ocr / redact_handwriting / redact_faces /
  output.images / output.everything / output.combine), `passwords`
  (top-level filename→password map — may now hold secrets, not just
  sensitive terms), `custom_patterns`; loaded by default on every run
  (`--config` overrides; missing explicit config = hard error). Gitignored
  and auto-created by run.sh from `config/redact_config.example.yaml` —
  edit the example for template changes; never commit or echo the
  personal copy's contents, and NEVER print `passwords` values anywhere
  (`--check-config` shows a count only). All settings documented in
  `docs/CONFIGURATION.md` — keep it in sync. `--check-config` prints the
  fully resolved config for auditing.
- README is deliberately simple/non-technical (drop in input/, run.sh,
  collect from output/, edit config) — technical detail belongs in docs/.
- `input/`, `output/` — user documents; ALL contents are gitignored
  (`input/*`, `output/*`, only `.gitkeep` tracked) and must stay that way.
- QR/barcode redaction uses zxing-cpp (unpinned on Python 3.13; the old
  `<2.3` pin was only needed for macOS system Python 3.9, since removed).
  Do NOT switch to pyzbar/zbar: Homebrew zbar 0.23.93 segfaults decoding
  QR codes on this machine (even `zbarimg` crashes), and a C-level crash
  can't be caught in Python.
