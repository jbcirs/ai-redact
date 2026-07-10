# Plan: Multi-Format Support, Python Upgrade & Detection Hardening

Status: **EXECUTED** 2026-07-09 (Phases 1–3 core; see §8 for what shipped
and deviations). Decisions locked in §7.
Author: drafted 2026-07-09, reviewed/grilled same day, executed same day
by a lead + three parallel builder agents + an independent plan-review
agent whose findings are folded into §8.

---

## 0. Why-names-were-missed incident — root cause & fixes (DONE)

Investigated first because it gates everything else: adding formats is
pointless if detection silently under-redacts.

**Root cause:** `custom_terms` in the user's config was organized into
groups (`names:`, `addresses:`, …) — a natural structure the template's
own comment headers suggest. The loader expected a flat list; iterating a
mapping yields its keys, so the tool matched the literal words "names",
"addresses", … and never saw the 22 real terms. Nothing was hardcoded;
the config was loaded (the `Config :` line proves it) — the shape was
misread, silently.

**Fixed (shipped today):**
1. `flatten_terms()` — accepts flat lists, grouped mappings, and nesting;
   used for both `custom_terms` and `exclude_terms`.
2. Whitespace-tolerant term matching — `"Jane Smith"` now matches across
   line breaks and double spaces in extracted PDF text.
3. Fail loudly: a non-empty `custom_terms` that yields zero usable terms
   is now a hard error; unusable terms are reported, never dropped
   silently. Template documents both shapes.
4. Verified: all 22 terms load; every term present in test files 01–05
   matches; full batch = 8/8 PASS including the OCR'd scanned letter.

**Remaining hardening (Phase 1 of this plan):**
- Config schema lint: warn on unknown top-level keys and wrong value
  types (a typo like `custom_term:` currently does nothing, silently).
- `--check-config` command: prints exactly what was loaded — term count,
  each switch, active preset — so users can audit without running a file.
- A tiny self-test corpus + `scripts/test.sh` that regenerates samples,
  runs the batch, and asserts every planted identifier was caught
  (today's bug would have been caught by exactly this).

---

## 1. Python runtime upgrade (prerequisite)

**Move from macOS system Python 3.9.6 → Homebrew `python@3.13`.**

Why 3.13 and not newer/older: 3.9 is EOL (Oct 2025) and already forced a
version pin (`zxing-cpp<2.3`); 3.13 is mature (~2 years old) with binary
wheels for every dependency in this plan; 3.14 is still missing wheels
for some scientific/imaging packages.

Verified wheel availability for 3.13 (arm64 macOS): PyMuPDF ✓, PyYAML ✓,
Pillow (incl. AVIF) ✓, zxing-cpp latest ✓ (pin removed), qrcode ✓,
pillow-heif ✓, rawpy ✓, openpyxl ✓ (pure), python-docx ✓ (pure).

Implementation:
- `scripts/run.sh`: prefer `$(brew --prefix)/opt/python@3.13/bin/python3.13`;
  install via `brew install python@3.13` if absent (~15 MB, quick);
  auto-recreate `.venv` when its Python doesn't match (delete + rebuild —
  the venv holds nothing precious).
- Remove the `zxing-cpp<2.3` pin from requirements.txt.
- Code is already 3.13-clean (no removed-API usage); full batch test after
  swap is the acceptance gate.
- Fallback: if Homebrew is missing, system 3.9 still works for the current
  PDF feature set; new-format features may require 3.13 (checked at
  startup with a clear message, never a crash).

---

## 2. Architecture: format router + handlers

Detection is already format-agnostic (`find_matches_in_text`, config,
presets know nothing about PDFs). The refactor isolates format I/O:

```
src/
├── redact.py            # CLI + router (thin)
├── detection.py         # patterns, presets, config loading  (extracted, unchanged)
├── report.py            # unified report writer              (extracted)
└── handlers/
    ├── pdf.py           # today's pipeline (redact + OCR + links + verify)
    ├── text.py          # .txt .md .log .json .yaml .xml (+ unknown-but-text)
    ├── csv_.py          # .csv .tsv  (cell-aware)
    ├── excel.py         # .xlsx      (cell-aware, native out)
    ├── image.py         # rasters → OCR boxes + barcode + metadata strip
    └── convert.py       # office/vector → PDF → pdf.py
```

Router rules:
- Select handler by **magic bytes first, extension second** (extensions
  lie; a "*.png" that's really a PDF must hit the PDF handler).
- Unknown type: if content sniffs as UTF-8/ASCII text → text handler with
  a warning; otherwise **refuse loudly** (exit 4, listed "Unsupported" in
  the batch summary). We never guess at binary formats.
- Every handler must implement `scan / redact / verify / report` — a
  handler without post-redaction verification does not ship.
- Batch summary gains an "Unsupported" bucket (exit code 4).
- Output naming: `<stem>_<origext>_redacted.<outext>` when the output
  extension differs from the input (prevents `foo.docx`/`foo.pdf`
  colliding in `output/`).

**Output-format policy (decided):** the tool's job is *prep for AI
upload*, not document editing. **Default output is PDF** for anything
converted, with user choice where the original format can be rewritten
safely:

```yaml
options:
  output:
    documents: pdf        # pdf (default) | original — original only where
                          #   a safe writer exists (txt, csv, xlsx); docx
                          #   stays pdf-only (hidden-data risk, §3.4)
    images: original      # original (default) | png | pdf
```

- Text/CSV always support original-out (it's the natural form).
- Images: same format back by default; `png` standardizes everything
  lossless; `pdf` wraps the redacted image in a one-page PDF.
- Formats with no safe write path (RAW, PSD, AI, EPS, SVG, docx) ignore
  `original` and use their stated fallback, with a report note — never a
  silent format switch.

---

## 3. Format matrix

### 3.1 Text-likes — native in/out (Phase 1, easy, high value)

| Format | Read/write | Notes |
|---|---|---|
| .txt .md .log | stdlib | replace match with `█`; trivial verify |
| .json .yaml .xml .html | stdlib | treated as text; also scan `href`/`mailto:` attributes in HTML |
| .csv .tsv | `csv` stdlib | per-cell matching so columns never break; quoting preserved |

### 3.2 Spreadsheets (Phase 2)

| Format | Plan |
|---|---|
| .xlsx | Native out via `openpyxl`. Scan **all** sheets incl. hidden, cell values, comments, sheet names, defined names; strip core properties. **Output is values-only** (formulas dropped) — otherwise `=A1&B1` can reconstruct a redacted cell. Loud note in report. |
| .xls (legacy) | No safe writer. Convert → PDF (§3.4) or refuse with "save as .xlsx first". |
| .numbers | Unsupported (no reliable local reader) — refuse with "export as xlsx/PDF". |

### 3.3 Images (Phase 1 core, Phase 3 exotics)

Pipeline: open → OCR (Tesseract via PyMuPDF at 300 dpi) → locate matches →
paint black boxes over those pixel regions → decode barcodes/QR (zxing-cpp,
already in) and blank them → **strip ALL metadata** (EXIF/GPS, XMP, IPTC —
for photos this is often the biggest leak: exact home coordinates) →
re-encode → verify by re-OCR + re-barcode-scan of the output.

| Format | Read | Write back | Phase | Notes |
|---|---|---|---|---|
| .jpg .jpeg | Pillow | same format (quality 95) | 1 | recompression noted in report |
| .png .bmp | Pillow | same format, lossless | 1 | |
| .tif .tiff | Pillow | same format | 1 | multi-page: every page processed |
| .webp | Pillow | same format | 1 | |
| .gif | Pillow | same format | 1 | animated GIFs are **flattened to the first frame** (decided §7); report notes the dropped frames |
| .heic .heif | pillow-heif | HEIC out possible; **default JPEG out** (recipient compatibility) | 2 | |
| .avif | Pillow 11 native | same format | 2 | |
| .ico | Pillow | same format | 3 | all embedded sizes processed; low value, nearly free |
| .cr2 .nef .arw .dng | rawpy (libraw) | **impossible** → high-quality JPEG/TIFF out | 3 | see §4; embedded thumbnails + serial numbers stripped by conversion |
| .cr3 | rawpy if libraw ≥0.21 | same | 3 | needs a 30-min spike on a real CR3 before promising |
| .psd | Pillow (flattened composite) | **no PSD out** → PNG/TIFF out | 3 | flattening is the *point*: hidden layers can hold PII invisible in the composite |
| .svg | rasterize (resvg/cairosvg) | **no SVG out** → PNG out | 3 | see §4 |
| .ai | PyMuPDF (modern .ai embeds PDF) | **no AI out** → PDF out | 3 | see §4 |
| .eps | Ghostscript → PDF | PDF out | 3 | adds `brew install ghostscript` (installed on demand, not by default) |

### 3.4 Office documents — Word AND PowerPoint (Phase 2)

**Decided (§7): no required dependency on installed Word/Excel/PowerPoint.
Tiered converter, pure Python first. Output is always PDF.**

There is no pure-Python library that renders Office files to PDF with
full fidelity (docx2pdf drives installed Word; LibreOffice is a ~700 MB
app; the "no-install" commercial SDKs are cloud services — banned by the
local-only rule). But full fidelity isn't the goal — *content-faithful
redaction* is. So:

- **Tier 1 (default, zero installs — pure Python):** read content with
  `python-docx` / `python-pptx` and render a clean, simplified PDF with
  PyMuPDF, then run the existing verified PDF pipeline.
  - Word: paragraphs, tables, headers/footers, footnotes, embedded
    images — walked **explicitly**; anything the reader can't reach
    (e.g. exotic text boxes, SmartArt text) is *omitted from the output*
    (safe direction — omitted content can't leak) and **counted in the
    report** ("2 elements could not be converted") so loss is never
    silent.
  - PowerPoint: slide text frames, tables, speaker notes, embedded
    images — one PDF page per slide (+notes section).
  - Layout is simplified, not pixel-identical. Acceptable for AI-upload
    prep; the report states which tier converted the file.
- **Tier 2 (auto-detected fidelity upgrade):** if Microsoft Word /
  PowerPoint (present on this Mac) or LibreOffice is installed, offer
  true rendering via automation — config
  `options: office_converter: auto | simple | msoffice | libreoffice`
  (default `auto` = best available). MS Office automation first accepts
  all tracked changes and strips comments in a **temp copy** before
  converting; triggers a one-time macOS permission dialog (documented).
- Legacy binaries (.doc .ppt .xls) have no pure-Python reader worth
  trusting → Tier 2 only, else refuse with "save as .docx/.pptx/.xlsx".
- .rtf: macOS built-in `textutil` converts rtf→html→ text pipeline, or
  Tier 2. .odt/.odp: Tier 2 (LibreOffice) only.
- .pages/.key/.numbers: refuse with "export as PDF/docx" (unchanged).

**Why not native .docx/.pptx out (decided — PDF only):** a .docx carries
tracked changes, comments, footnotes, custom XML, embedded objects, and
metadata. Redacting visible runs while any of those leak is a *false
promise* — the failure mode this project exists to prevent. PDF-out
flattens everything through a pipeline we already verify.

### 3.5 Everything else

- .eml/.msg (email), .epub: plausible Phase 4 (text-based cores); noted,
  not committed.
- Archives (.zip): out of scope — users redact the contents.
- Password-protected/DRM files of any type: refuse with a clear message
  (a `--password` option is a possible Phase 4 nicety).

---

## 4. What cannot be supported (told straight)

1. **RAW write-back (.cr2/.cr3/.nef/.arw/.dng):** libraw and every other
   local library is decode-only; camera RAW is proprietary and
   effectively write-proof. Output is a converted JPEG/TIFF. If you need
   a redacted *RAW*, it does not exist as a concept.
2. **.psd out:** no reliable local writer, and layered output would defeat
   redaction anyway (hidden layers). Flattened PNG/TIFF out only.
3. **.ai out:** an .ai file contains Adobe's private editing data
   alongside its PDF preview; "redacting" the preview while private data
   remains would leak. PDF out only — and only for PDF-compatible .ai
   files (Illustrator ≥ v9 default); older ones are refused.
4. **SVG out:** text converted to path outlines is invisible to text
   matching, embedded scripts/base64 images can hide anything. Native SVG
   redaction cannot be made trustworthy → rasterize to PNG only.
5. **Faces and signatures in photos:** face detection/blurring is a
   different product; signatures are images, not text. Reports keep
   flagging image regions for manual review. **Handwriting** is now a
   committed Phase 4 item (decided §7) — Tesseract is too weak for it,
   but Apple's on-device Vision framework (`VNRecognizeTextRequest`,
   via pyobjc) reads handwriting locally with decent accuracy and fits
   the local-only rule. It is *not* trivially added today (coordinate
   mapping, pyobjc plumbing, accuracy thresholds), so it's scheduled,
   not slipped into Phase 1.
6. **iWork (.pages/.numbers/.key):** no reliable local converter exists
   outside Apple's apps → refuse with "export as PDF/docx".
7. **Perfect recall:** OCR on skewed/blurry photos will miss text.
   Verification re-OCRs the output, but what OCR can't read, it can't
   verify either. The report's manual-review warnings stay.

---

## 5. Grill — attacks on this plan, and resolutions

| # | Attack | Resolution |
|---|---|---|
| 1 | *"Convert-to-PDF launders hidden Word content into the output"* — tracked-changes text can appear in the rendered PDF. | Word automation accepts all revisions + removes comments in a **temp copy** before converting (documented AppleScript step), never touching the original. |
| 2 | *"JPEG re-encode degrades the user's photo"* | quality 95 + report note; PNG/TIFF (lossless) config option for image output. |
| 3 | *"Values-only xlsx silently breaks someone's model"* | It's stated on the report in caps; alternative (keep formulas) is strictly worse — reconstructs redacted data. Right trade-off for share-prep. |
| 4 | *"Extension spoofing routes a PDF to the text handler"* | Magic-byte sniffing first; extension is only a tiebreaker. |
| 5 | *"One 500 MB TIFF or 60 MP RAW blows up memory/time"* | Per-file size guard with a warning; RAW decode is seconds on Apple Silicon; OCR dpi capped at 300. |
| 6 | *"GIF frame redaction re-encodes palettes badly"* | Accepted cosmetic risk; correctness first. Report notes animated re-encode. |
| 7 | *"Two inputs collide on output name"* (foo.docx→pdf vs foo.pdf) | `<stem>_<origext>_redacted.<ext>` naming rule. |
| 8 | *"Router grows a handler without verification"* | Handler interface makes `verify` abstract-required; CI test (scripts/test.sh) runs every handler against a planted-PII fixture. |
| 9 | *"Homebrew python upgrade breaks the venv mid-flight"* | run.sh compares `.venv` python version and rebuilds atomically before any file is touched; nothing user-precious lives in .venv. |
| 10 | *"'Any other document type' is an unbounded promise"* | Scoped: known formats per this matrix, text-sniffed fallback, loud refusal otherwise. The README will list exactly what's supported. |
| 11 | *"AppleScript automation prompts scare users / fail headless"* | One-time permission documented in runbook; failure falls back to the refuse-with-instructions path, never a hang (60 s timeout). |
| 12 | *"Today's config bug shows detection can silently degrade — new formats multiply that risk"* | §0 hardening lands in Phase 1 *before* new formats: config lint, `--check-config`, and a planted-PII regression test that every handler must pass. |
| 13 | *"Tier-1 'simplified' Office conversion silently drops content users needed"* | Dropped-element counting is a hard requirement of the Tier-1 converter: the report always says what could not be converted. Omission is the safe direction (omitted ≠ leaked), but it must be visible. |
| 14 | *"Tier-1 output tempts users to think it IS the document"* | Report header names the converter tier and states "layout simplified; content-faithful". `office_converter: msoffice` is one config line away when fidelity matters. |

---

## 6. Phasing & rough effort

Order: easiest-first (decided §7).

| Phase | Scope | Effort |
|---|---|---|
| 1 | Python 3.13 upgrade; handler/router refactor; text + csv + core rasters (jpg/png/tiff/webp/bmp/gif-flattened) with metadata stripping; output-format options; config lint + `--check-config` + regression test script | 1–2 sessions |
| 2 | Word + PowerPoint via Tier-1 pure-Python converter (python-docx/python-pptx → simplified PDF) with Tier-2 auto-upgrade (MS Office / LibreOffice if present); native values-only .xlsx; .heic/.avif | 1–2 sessions |
| 3 | RAW (incl. CR3 spike), PSD, SVG, AI, EPS, ICO; unknown-type sniffing polish | 1–2 sessions |
| 4 | **Handwriting OCR via Apple Vision framework (committed)**; .eml/.msg, .epub, password-protected files, native-docx investigation, face-blur investigation (optional) | 1–2 sessions + on request |

Each phase ends with: full batch run over an expanded `input/` fixture
set, every file PASS, docs updated (README simple list, CONFIGURATION,
RUNBOOK, ARCHITECTURE, CLAUDE.md).

---

## 7. Decisions (answered 2026-07-09)

1. **Office conversion**: no hard dependency on installed Word/Excel —
   pure-Python Tier-1 converter is the default; installed MS Office /
   LibreOffice are auto-detected fidelity upgrades (§3.4). PowerPoint
   included.
2. **Word/PowerPoint output**: PDF. Locked.
3. **Excel/output generally**: user-choosable `output.documents:
   original` where a safe writer exists; **default PDF**.
4. **Images**: keep original format by default; `output.images: png`
   standardizes, `pdf` wraps. RAW/PSD convert regardless (JPEG default,
   PNG/TIFF via the same option).
5. **Animated GIFs**: flatten to first frame.
6. **Handwriting**: committed as Phase 4 via Apple Vision (assessed as
   not safely doable "now" — see §4.5). Faces/signatures remain
   flag-for-review only.
7. **Phase order**: easiest first (as tabled in §6).

---

## 8. Execution record (2026-07-09)

**Shipped:** Python 3.13 runtime (run.sh bootstraps Homebrew python@3.13,
rebuilds .venv on version mismatch; zxing-cpp unpinned); format router in
src/redact.py (magic-bytes first); handlers: text (.txt .md .log .json
.yaml .yml .xml .html .htm), csv/tsv, xlsx (native values-only),
docx/pptx (Tier-1 pure-Python → simplified PDF), images (.jpg .jpeg .png
.gif .webp .bmp .tif .tiff .heic .heif .avif .ico .psd + RAW decode
.cr2 .cr3 .nef .arw .dng) with EXIF/GPS stripping and write-back;
`--check-config`; config lint (unknown keys warn); `output.images`
option (original|png|pdf); output naming `<stem>_<origext>_redacted.<ext>`;
run.sh processes every file in input/ with an Unsupported bucket (exit 4);
scripts/test.sh + tests/check_outputs.py planted-PII regression suite
(20 artifacts, all formats, PASS).

**Plan-review findings (independent agent) — all addressed:** run.sh
python bootstrap + glob gaps (fixed); per-cell scanning disabling labeled
detectors in tables → **header-as-context rule** (each data cell also
scanned as "header: value"); bare digit-run pattern excluded for tabular
formats (financial preservation); python-docx has no footnote API →
raw word/footnotes.xml parsing; textbox/SmartArt counting via raw XML
scan; xlsx formula cells with no cached value counted and reported;
xl/drawings presence detected and reported; HTML entity/percent-encoded
PII decoded and redacted in all spellings; report schema split
(write_native_report for native handlers).

**Bugs found by the test suite in the EXISTING tool:** (1) phones followed
by sentence punctuation never matched — the trailing guard `(?![\d.\-])`
rejected "…9999." — fixed to reject only digit continuations; same fix on
the bare digit-run pattern; (2) values wrapped across line breaks could
evade single-whitespace separators — detection now also scans a
whitespace-normalized copy of every text.

**Deviations from the letter of the plan:**
- `output.documents` is not a config option: text/csv/xlsx always output
  their own format (they have safe writers — "original where possible"
  satisfied by default), docx/pptx always output PDF (locked decision).
  Forcing a .txt into a PDF served no one.
- Tier-2 office conversion (MS Office/LibreOffice automation) not built —
  Tier 1 covers the AI-upload use case; the `office_converter` config key
  is accepted (reserved) but only tier1 exists. Revisit on demand.
- .ai files that are PDF-compatible route through the PDF pipeline
  automatically (magic bytes); EPS and SVG remain unsupported (exit 4
  with guidance) — the ghostscript/cairo system dependencies weren't
  justified this round. RAW/PSD read paths are implemented but
  fixture-untested (RAW/PSD cannot be synthesized locally) — best-effort.

**Not done (later phases):** handwriting (Phase 4, Apple Vision),
.eml/.msg/.epub, password-protected files, Tier-2 converters, EPS/SVG.
