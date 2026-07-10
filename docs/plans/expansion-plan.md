# Plan: Format Expansion — Legacy Office, RTF, Email, EPUB, Handwriting, Faces, Passwords, Combine-to-One-PDF

Status: **EXECUTED** 2026-07-10 (all of Phase A/B/C; see §8 for what
shipped, deviations, and bugs found). Decisions locked 2026-07-10 (§1).
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

## 2a. Pre-implementation amendments (review pass, 2026-07-10)

Found while re-reading this plan against the current router/handler code,
before writing any of it. These are binding — implementation must follow
them, not the original prose where they conflict.

1. **Existing bug, fix as part of Workstream A**: `route_input()` in
   `src/redact.py` falls through to `_sniffs_as_text(path)` for any
   extension it doesn't recognize. RTF is plain ASCII, so `.rtf` files
   TODAY silently get routed to `text_handler`, which would read/write RTF
   control codes as if they were plain content — corrupting the file
   structure rather than cleanly redacting it. Workstream A must add
   explicit detection (RTF: `{\rtf1` magic; DOC: OLE2 `D0CF11E0` magic;
   ODT: zip whose `mimetype` entry is
   `application/vnd.oasis.opendocument.text`) BEFORE the text/UTF-8
   fallback, not after.
2. **Extract the shared PDF renderer first.** `office_handler.py`'s
   `_PdfFlow` (text-to-PDF flow-and-wrap engine) is module-private, but
   Workstreams E (email), F (epub), and H (everything→PDF) all need
   "render text to PDF." Move it to `src/handlers/pdf_render.py` as a
   public, reusable class before starting any of E/F/H — otherwise three
   workstreams reimplement the same wrapping/pagination logic
   independently and drift.
3. **`--check-config` must never print password values.** Once §3.G lands
   a `passwords:` map in the config, `--check-config`'s existing
   print-every-term behavior (which is fine for `custom_terms`/
   `exclude_terms`) must NOT apply to `passwords:` — show a count only
   (`Passwords configured: N file(s)`), never the values, matching the
   "never echoed into reports or logs" rule in §6 grill item 6.
4. **`lint_config` needs new known-key entries** or the linter will warn
   "unknown option" the moment anyone adopts the new config surface:
   top-level `passwords`; `options:` keys `handwriting_ocr`,
   `redact_handwriting`, `redact_faces`; `options.output:` keys
   `everything`, `combine`.
5. **Subprocess safety for B (LibreOffice) and C (AppleScript).** Both
   invoke external processes with a user-supplied filename in the
   argument. Always use arg-list `subprocess.run([...])`, never
   `shell=True` with string interpolation. `soffice` must be given an
   isolated `-env:UserInstallation=file://<tmp-profile>` per run so a
   batch loop over many files doesn't corrupt or collide with the user's
   real LibreOffice profile (or with itself if a later phase parallelizes
   the batch loop).
6. **New exit code 5 must be threaded through `scripts/run.sh`'s exit-code
   classification** (currently only handles 0/2/3/4) and into the
   exit-code tables in README.md/docs/RUNBOOK.md/docs/CONFIGURATION.md —
   it is a new outcome bucket in the batch summary, not a variant of an
   existing one.

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

### C. MS Office automation (Tier-2, fidelity path) (Phase B) — DESCOPED

**Descoped during implementation (2026-07-10).** Live-tested against the
real, installed Microsoft Word (365) on this Mac: every documented/
historical AppleScript form of the `save as ... file format format PDF`
command (direct, inside a `tell document`, numeric format code 17,
generic Standard-Suite `save … in … as "PDF"`) either raised
`active document doesn't understand the "save as" message` or silently
no-opped (no file written, exit 0 — the dangerous failure mode: a report
would have claimed success with nothing produced). This Word build's
scripting dictionary does not expose a working document-level PDF export
via AppleScript. Per CLAUDE.md's fail-loud/never-fake-success rule, this
plan does **not** ship guessed AppleScript against a redaction tool's
conversion path.

**Resolution**: `office_converter: msoffice` is accepted as a config
value but always raises a clear, actionable error ("MS Office automation
is not implemented — this Word/Excel/PowerPoint build has no working
AppleScript PDF export; use `office_converter: libreoffice` or `simple`
instead"), never a silent fallback. `auto` mode skips the msoffice tier
entirely (tries libreoffice, then Tier-1 `simple` for .docx/.pptx, else
refuses). LibreOffice (§3.B) remains the sole Tier-2 fidelity path and
already covers every extension this workstream would have; no format in
§4's master list loses coverage. Revisit only if a future Word/Office
release restores a verified, working AppleScript (or JXA/Automator)
export path — re-probe live before re-attempting, don't re-guess.

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

**Execution note (implemented, 2026-07-10)**: All three switches are
implemented and live-verified. `handwriting_ocr` and `redact_handwriting`
are fixture-tested end to end (cursive-font `Snell Roundhand` image,
planted name recognized/redacted, verify PASS) — see
`tests/make_vision_fixtures.py`. The coordinate-mapping formula (Vision
normalized bottom-left → PyMuPDF top-left points) was spiked live before
writing `vision_helper.py` and confirmed against a known text placement.
`redact_faces` is integration-tested only (runs cleanly end to end, 0
faces on a non-face image, no crash) — **not accuracy-tested**: no
synthetic image reliably triggers Vision's face detector (it's trained
on real photos) and no real photo is available offline to build a
fixture with (same category of gap as `.msg` in §3.E — implemented
against the documented API, unverified by fixture). A page is also no
longer misclassified as "unreadable" (exit 2) or hard-stopped as "no
searchable text" (exit 2, output deleted) when Vision successfully
handled it but Tesseract's separate OCR pass could not — found and fixed
while building this, since a cursive-handwriting image is exactly the
case where Tesseract fails but Vision succeeds.

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

**Execution note (implemented, 2026-07-10)**: `.eml` (stdlib `email`) is
implemented AND fixture-tested end to end, including a text attachment
proving recursive redaction (`tests/make_email_fixtures.py`,
`scripts/test.sh`). `.msg` (`extract-msg`) is implemented against the
package's documented read API but has **no automated fixture** — no
pure-Python `.msg` writer exists on this Mac to construct one (unlike
`.eml`, which is plain text and trivial to build; unlike the AppleScript
case, this isn't "doesn't work," it's "not independently verified").
Per the "no fixture, not done" rule (§6 grill item 12), `.msg` support
should be treated as **implemented but unverified** until exercised
against a real `.msg` file (e.g. one exported from Outlook/Mail.app) —
do that before relying on it for anything sensitive.

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

## 8. Execution record (2026-07-10)

All of Phase A/B/C implemented in one pass, per the review+all-phases
decision at kickoff. `./scripts/test.sh` green at 32 planted-PII
artifacts (up from 20 at the end of format-support-plan.md); `pip show`
confirms every new dependency installed and import-clean.

**Shipped:**
- **Pre-implementation amendments (§2a)**, all applied: shared
  `src/handlers/pdf_render.py` (extracted from office_handler's private
  `_PdfFlow`, now also exposes `html_to_text()` and
  `render_to_pdf_bytes()`); `lint_config` knows every new key;
  `--check-config` shows a password count, never values; soffice/
  osascript-adjacent code uses arg-list subprocess with an isolated
  LibreOffice profile dir; exit code 5 threaded through run.sh.
- **A — legacy Office + LibreOffice + everything/combine**:
  `legacy_office_handler.py` (.doc/.odt/.rtf via `textutil`),
  `libreoffice_handler.py` (.xls/.ppt/.odp + Tier-2 fidelity),
  `resolve_office_handler()` (auto/simple/libreoffice/msoffice
  resolution), run.sh's consent-gated LibreOffice install prompt
  (cached in `config/.libreoffice_declined`), `output.everything`/
  `output.combine` + `src/combine_outputs.py` (TOC, whole-document
  re-verify, refuses to merge a FAILed file).
- **B — email/epub/passwords**: `email_handler.py` (.eml full stdlib
  parse + attachment recursion depth-2/100MB/50-count caps; .msg via
  extract-msg), `epub_handler.py` (spine-order chapters, DRM refused),
  `--password`/config `passwords:` map + `EncryptedFileError` → exit 5
  for PDF (PyMuPDF `authenticate`) and Office (msoffcrypto-tool).
- **C — Apple Vision**: `vision_helper.py` (face detection, text/
  handwriting recognition, normalized-bottom-left → PyMuPDF-top-left
  coordinate mapping), wired into `process_pdf`'s per-page loop and
  `verify_output`'s re-verify pass, gated by three independent
  default-off switches.
- Regression coverage: `tests/make_legacy_office_fixtures.py`,
  `make_email_fixtures.py`, `make_epub_fixtures.py`,
  `make_encrypted_fixtures.py`, `make_vision_fixtures.py` — all wired
  into `scripts/test.sh`.

**Descoped / honestly incomplete (see workstream sections for detail,
not repeated here):**
- §3.C MS Office AppleScript automation — live-tested against this
  Mac's real Word install, doesn't work in this build. `msoffice` errors
  clearly instead of silently falling back to something else.
- `.msg` (§3.E) and `redact_faces` (§3.D) are implemented against their
  respective APIs but have **no automated fixture** — no pure-Python
  `.msg` writer and no real face photo available offline to build one
  with. Both are integration-verified (run cleanly, correct plumbing),
  not accuracy-verified. Provide real test files before relying on
  either for anything sensitive.

**Bugs found in the EXISTING tool while building this (both fixed,
neither related to the plan's own scope):**
1. `route_input()`'s text-sniff fallback would have silently misrouted
   `.rtf` files to `text_handler` (RTF is ASCII) — control codes read/
   written as plain content, corrupting the file. Fixed by intercepting
   RTF on magic bytes/extension before the fallback (§2a.1 anticipated
   this; found and fixed during §3.A).
2. `resolve_outputs()`'s report-path derivation used `output_path.stem`,
   which strips only the FINAL extension — `foo_redacted.txt` and
   `foo_redacted.yaml` (same stem, different native-format extensions, a
   completely ordinary same-directory batch) collapsed to the identical
   `foo_redacted_report.txt`, each overwriting the other's audit trail.
   Found while building `combine_outputs.py`, which trusts each file's
   report to decide whether it's safe to merge — this collision would
   have let a FAILed file's stale PASS report wave it into a shared PDF.
   Fixed: report name now derives from the full output filename
   (extension included), in both `redact.py` and `combine_outputs.py`.
3. A page Vision successfully reviewed (handwriting/faces) but Tesseract
   could not OCR (e.g. cursive handwriting — exactly Vision's use case)
   was being misclassified as "unreadable" (exit 2) or, worse, tripping
   the "no searchable text at all" hard-stop that deletes the output and
   claims nothing was redacted — even though Vision HAD redacted
   something. Fixed: Vision-reviewed pages count toward
   `total_text_chars` and are exempted from the Tesseract-only
   `scanned_pages` classification.

**Deviations from the letter of the plan:** none beyond the descopes
above — every other workstream ships as specified in §3, including the
naming conventions, caps, and report formats called out there.
