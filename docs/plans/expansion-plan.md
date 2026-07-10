# Plan: Format Expansion — Legacy Office, RTF, Email, EPUB, Handwriting, Faces, Passwords, Combine-to-One-PDF

Status: **PROPOSED** — decisions locked 2026-07-10 (§1); not yet implemented.
Builds on `format-support-plan.md` (EXECUTED 2026-07-09) and its handler
contract in `handler-spec.md`. All hard constraints in CLAUDE.md apply:
local-only, permanent redaction, fail-loud, never redact financial data,
verification required for every handler, `./scripts/test.sh` is the gate.

---

## 1. Decisions (answered by John, 2026-07-10)

1. **LibreOffice** (needed for .xls/.ppt/.odp): auto-install via Homebrew,
   but **ask the user first** — run.sh prints what it is (~700 MB, free,
   fully offline) and prompts install / skip. Skip → those files go to the
   Unsupported bucket with the exact `brew install --cask libreoffice`
   command. Non-interactive runs never install silently.
2. **Faces & handwriting**: opt-in config switches, OFF by default —
   `redact_faces`, `redact_handwriting` (blanket), `handwriting_ocr`
   (targeted). See §4.
3. **Combine into one PDF**: an EXTRA file (`output/combined_redacted.pdf`,
   filename order) alongside the individual outputs, never instead of them.
4. **Email attachments**: extracted and recursively redacted through the
   format router; outputs land alongside the redacted email body.

## 2. Verified feasibility (probed 2026-07-10 on this Mac)

- `textutil` (built into macOS) converts **doc, odt, rtf → docx** — the
  entire legacy Word family becomes a zero-install preprocessing step into
  the existing Tier-1 docx pipeline.
- LibreOffice is NOT currently installed; disk has 1.6 TB free.
- MS Word and Excel ARE installed (PowerPoint presence unknown — detect at
  runtime).
- `pyobjc-framework-Vision` 12.2 available — Apple's on-device Vision
  framework (handwritten text recognition + face rectangle detection),
  fully local.
- `extract-msg` available for .msg; `.eml` parsing is Python stdlib.
- PyMuPDF already exposes `needs_pass` / `authenticate` — encrypted PDFs
  need only plumbing. Encrypted Office files need `msoffcrypto-tool`
  (pure Python; verify with a spike).

## 3. Workstreams

### A. Legacy Word family via textutil — zero install (Phase A)

`.rtf`, `.doc`, `.odt` → `textutil -convert docx` into a temp copy →
existing Tier-1 docx handler → PDF out. textutil is an Apple system
binary operating locally; the temp copy lives in the run's temp dir and
is deleted after. Report names the conversion chain
(`textutil → tier1-python-docx`). Failure of textutil (corrupt file) →
exit 4 with guidance, never a silent skip.

### B. LibreOffice converter — consent-gated (Phase A)

- run.sh: when the input batch contains `.xls`/`.ppt`/`.odp` (or the user
  set `office_converter: libreoffice`) and LibreOffice is absent →
  interactive prompt (install ~700 MB / skip). TTY absent → treat as skip.
- Conversion: `soffice --headless --convert-to pdf --outdir <tmp>` per
  file with a timeout (120 s), then the standard PDF pipeline. LibreOffice
  runs fully offline.
- This also completes **Tier-2** for the whole Office family:
  `office_converter: auto | simple | msoffice | libreoffice` becomes real.
  `auto` = msoffice if the needed app is installed, else libreoffice if
  installed, else simple (Tier-1) for docx/pptx, else refuse for formats
  Tier-1 can't read.

### C. MS Office automation (Tier-2, fidelity path) (Phase B)

AppleScript automation of the user's installed Word/Excel (PowerPoint if
present): open a **temp copy**, accept all tracked revisions, delete
comments, save-as PDF, close without touching the original. One-time
macOS permission dialog documented in the runbook. Per-app availability
detected at runtime; a missing app falls back down the `auto` chain with
a printed note (loud, never silent).

### D. Handwriting + faces via Apple Vision (Phase C)

New optional dependency: `pyobjc-framework-Vision` (+ Quartz). Three
independent, opt-in config switches (all default false):

```yaml
options:
  handwriting_ocr: false    # recognize handwritten text (Vision) and
                            # redact MATCHES only (names, SSNs, …)
  redact_handwriting: false # black out EVERY handwriting-shaped region —
                            # the honest signature mechanism (see §5)
  redact_faces: false       # black out detected face rectangles
```

- Integration point: the image/scanned-page path. Vision runs on the
  rendered page raster; normalized coordinates map to page rects; regions
  become standard redaction annotations (permanent pixel blanking).
- `handwriting_ocr` feeds recognized text through the SAME matcher
  (categories + custom_terms + exclude_terms, honored in verification —
  CLAUDE.md invariant).
- Faces are covered with solid black boxes, not blurs — pixelation is
  partially reversible; boxes are not. The option keeps the honest name
  `redact_faces`.
- Verification: re-run Vision on the output image; matched
  text/faces/handwriting found again → verify FAIL (exit 3).
- Report lines: "N face(s) redacted", "N handwriting region(s) redacted",
  plus the standing caveat that detection is best-effort (§5).

### E. Email containers (Phase B)

- `.eml`: stdlib `email` parsing. `.msg`: `extract-msg`.
- Body + headers (From/To/Cc/Subject/Date — headers are PII-dense and are
  scanned like any text) render to PDF via the Tier-1 text renderer and
  ride the PDF pipeline.
- Attachments: extracted to the run temp dir and fed through the format
  router recursively (decision §1.4). Outputs named
  `<email-stem>_att1_<name>_redacted.<ext>`. Unsupported attachment types
  land in the Unsupported bucket, listed in the email's report. Recursion
  depth capped at 2 (an email attached to an email) — deeper nesting is
  reported and skipped.

### F. EPUB (Phase B)

Zip of XHTML. Convert handler: walk spine order, extract chapter text
(reusing the text handler's entity-decoding scan), render to PDF via the
Tier-1 renderer → PDF pipeline → PDF out. No native-epub output —
repackaging the zip risks leaking metadata/unscanned resources. DRM'd
epubs: refuse (see §5).

### G. Password-protected files (Phase B)

- PDFs: `--password` CLI flag; batch runs may also use a `passwords:` map
  in the config (`filename: password`). The config is already gitignored
  and holds sensitive terms; the docs add an explicit warning that it now
  may hold passwords too.
- Encrypted .docx/.xlsx/.pptx: decrypt to a temp stream via
  `msoffcrypto-tool`, then the normal handler. (Spike first — verify the
  library round-trips agile-encryption files.)
- Wrong/missing password → new exit code **5 = encrypted, could not
  open**, with its own batch-summary bucket. Never a silent skip.

### H. Everything-to-PDF + combine into one PDF (Phase A)

```yaml
options:
  output:
    everything: original   # original (default) | pdf — force ALL outputs
                           # to PDF (text/csv/xlsx rendered via the Tier-1
                           # renderer; images wrapped one per page)
    combine: false         # true = ALSO write output/combined_redacted.pdf
```

- `combine: true` (or `--combine` on run.sh) merges every redacted output
  into `combined_redacted.pdf` in filename order — an EXTRA artifact,
  individual outputs still written (decision §1.3). Native-format outputs
  are converted to PDF for the merge only.
- A `combined_redacted_report.txt` concatenates the per-file reports with
  a table of contents (file → page range in the combined PDF).
- The combined PDF is re-verified as a whole (one more pattern scan over
  the merged document) before it is reported PASS.
- Implementation: `src/combine_outputs.py` invoked by run.sh after the
  batch loop; single-file runs ignore combine.

## 4. Master supported-format list (after this plan)

| Family | Extensions | Path | Output |
|---|---|---|---|
| PDF | .pdf (+ PDF-compatible .ai) | native pipeline | .pdf |
| Word | .docx | Tier-1 / Tier-2 | .pdf |
| Word legacy | .doc, .odt, **.rtf** | **textutil → docx → Tier-1** | .pdf |
| PowerPoint | .pptx | Tier-1 / Tier-2 | .pdf |
| PowerPoint legacy | **.ppt, .odp** | **LibreOffice (consent-gated)** | .pdf |
| Excel | .xlsx | native (values-only) | .xlsx |
| Excel legacy | **.xls** | **LibreOffice (consent-gated)** or Excel automation | .pdf |
| Spreadsheet text | .csv, .tsv | native, header-as-context | same |
| Text | .txt, .md, .log, .json, .yaml, .yml, .xml, .html, .htm | native | same |
| Images | .jpg, .jpeg, .png, .gif, .webp, .bmp, .tif, .tiff, .heic, .heif, .avif, .ico | OCR pipeline, metadata stripped | same (options: png/pdf) |
| Photoshop / RAW | .psd, .cr2, .cr3, .nef, .arw, .dng | decode → OCR pipeline | .jpg/.png (no write-back exists) |
| Email | **.eml, .msg** | body→PDF + attachments recursed | .pdf (+ per-attachment) |
| Ebook | **.epub** (non-DRM) | chapters → Tier-1 → PDF | .pdf |
| Encrypted | password-protected PDF / Office | `--password` / config map | per underlying type |

README and docs/CONFIGURATION.md must carry this table verbatim
(simplified wording in README) — keeping the list current is part of the
definition of done for every future format change.

## 5. Not possible / honest limits (told straight)

1. **Signature-specific detection does not exist.** No local library
   reliably distinguishes a signature from other handwriting. The shipped
   mechanism is `redact_handwriting` (black out ALL handwriting-shaped
   regions) — it catches signatures but also handwritten notes. That is
   the honest ceiling.
2. **Face detection is best-effort, not an anonymization guarantee.**
   Vision misses heavily occluded/profile/low-res faces. The report
   counts what was redacted; the final human skim remains mandatory.
3. **DRM cannot and will not be broken.** DRM'd epubs/ebooks and
   rights-managed (Azure IP/AIP) Office files are refused with a clear
   message. Password-protected ≠ DRM: files you have the password for
   are fully supported (§3.G).
4. **iWork (.pages/.numbers/.key)** remains export-first — no reliable
   local reader exists outside Apple's apps. (LibreOffice's iWork import
   is too lossy to trust for redaction.)
5. **.xls/.ppt/.odp without LibreOffice**: if the user declines the
   install (their call per §1.1), these stay Unsupported — there is no
   trustworthy pure-Python reader for these binary formats.
6. **textutil fidelity**: .doc/.odt/.rtf go through Word-family
   conversion that can drop exotic embedded objects — the dropped-element
   counting rule from the Tier-1 converter applies (omitted ≠ leaked,
   never silent).

## 6. Grill — attacks on this plan, and resolutions

| # | Attack | Resolution |
|---|---|---|
| 1 | *"textutil might KEEP tracked changes/comments when converting .doc→.docx, and Tier-1 would then render them into the PDF"* | Spike first: convert a .doc with revisions + comments and inspect the docx. If they survive, strip them from the converted docx zip (delete comment parts, accept w:ins/w:del) before Tier-1. This is a gate, not an afterthought. |
| 2 | *"LibreOffice consent prompt breaks non-interactive runs"* | No TTY → auto-skip with the Unsupported message. The prompt result is cached in config (`office_converter: libreoffice` or a `libreoffice: declined` marker) so the user is asked once, not every run. |
| 3 | *"Recursive email attachments can explode (zip-of-emails-of-zips)"* | Depth cap 2, per-attachment size cap (100 MB), attachment count cap (50) — beyond caps: reported, skipped, exit 2-style warning bucket. |
| 4 | *"Vision coordinates are normalized and bottom-left origin; PyMuPDF is top-left points"* | Coordinate mapping gets its own unit test with a fixture image whose handwriting position is known; a mapping bug would redact the WRONG region, which verification would catch (text still present → FAIL), but the test catches it cheaper. |
| 5 | *"handwriting_ocr text can't be located precisely for partial matches"* | Vision returns per-observation bounding boxes; a match inside an observation redacts that observation's whole box (over-redaction is the safe direction; noted in report). |
| 6 | *"Passwords in the config file"* | Already-gitignored file, explicit docs warning, and passwords are never echoed into reports or logs. CLI `--password` preferred for one-offs. |
| 7 | *"Combined PDF could include a verify-FAILED file"* | combine refuses to include any file whose individual verify failed — it prints which file blocked the merge and exits 3. A combined artifact must be shareable as a unit. |
| 8 | *"everything: pdf silently changes xlsx/text output format users rely on"* | It's opt-in config, default `original`; the report header names the forced conversion. |
| 9 | *"AppleScript automation hangs on a Word dialog"* | 120 s timeout per file → kill the app process for that job, fall back down the auto chain, loud note. |
| 10 | *"New detection surfaces (Vision text, email headers) bypass exclude_terms"* | CLAUDE.md invariant: every new surface threads the same matcher (categories + exclude) through redaction AND verification. test.sh gains fixtures asserting exclude_terms are honored on the new surfaces. |
| 11 | *"msoffcrypto spike fails"* | Encrypted Office demotes to exit 5 with "decrypt manually and re-run" guidance; PDFs (the common case) still work via PyMuPDF. |
| 12 | *"Plan grows the format list but not the regression suite"* | Definition of done per phase: a fixture generator + check_outputs coverage for every new format (email with attachment, epub, rtf/doc/odt via textutil, encrypted pdf, combined output, Vision fixtures). No fixture, not done. |

## 7. Phases & rough effort

| Phase | Scope | Effort |
|---|---|---|
| A | textutil chain (.rtf/.doc/.odt); LibreOffice consent flow + .xls/.ppt/.odp; `everything: pdf` renderer + `combine` (+ merged report & re-verify); fixtures for all | 1–2 sessions |
| B | Email (.eml/.msg + recursive attachments), .epub, passwords (PDF + msoffcrypto spike, exit 5); MS Office Tier-2 automation; fixtures | 1–2 sessions |
| C | Apple Vision: handwriting_ocr, redact_handwriting, redact_faces; coordinate-mapping unit test; Vision fixtures (drawn "handwriting" via cursive font + a synthetic face image) | 1–2 sessions |
| — | Every phase ends: `./scripts/test.sh` green, `./scripts/run.sh` over input/ all PASS, README/CONFIGURATION/RUNBOOK/ARCHITECTURE/CLAUDE.md updated incl. the master format table | — |

Phase order rationale: A is highest value-per-effort and unblocks the
combine feature you asked for; C is last because Vision work has the most
unknowns (coordinate mapping, detection quality tuning).
