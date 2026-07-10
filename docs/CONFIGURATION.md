# Configuration Reference — ai-redact

Every setting the tool understands, in one place. The config file is
`config/redact_config.yaml` (created from
`config/redact_config.example.yaml` on first run, loaded automatically on
**every** run, gitignored so it can never be committed). Command-line flags
are listed at the bottom.

## Config file settings

### `preset`

Default document type when none is given on the command line.
One of `financial`, `medical`, `legal`, `general`.

```yaml
preset: general
```

Precedence: command line (`./scripts/run.sh financial` or `--preset`)
beats this setting; this setting beats the built-in default (`general`).

What each preset enables:

| Preset | Categories |
|---|---|
| `financial` | account_number, credit_card, routing_number, tax_id, ssn, address, phone, email, dob |
| `medical` | mrn, insurance_id, credit_card, dob, ssn, address, phone, email |
| `legal` | case_number, drivers_license, passport, ssn, dob, address, phone, email |
| `general` | every category |

### `categories`

Per-detector switches that override the preset on every run:

```yaml
categories:
  email: true      # true  = ALWAYS redact, whatever the preset says
  address: false   # false = NEVER redact, whatever the preset says
  # dob:           # commented out / omitted = the preset decides
```

All category names (see also `./scripts/redact.sh --list-categories`):

| Category | Detects | How |
|---|---|---|
| `email` | Email addresses | pattern |
| `phone` | Phone numbers (US formats, needs separators) | pattern |
| `ssn` | Social Security numbers | `###-##-####` anywhere; unformatted 9 digits only next to an SSN label |
| `tax_id` | EIN/TIN tax IDs | `##-#######` anywhere; other forms next to a label |
| `account_number` | Account numbers | labeled ("Account #: …"), masked (`****1234`), or standalone 8–17 digit runs |
| `credit_card` | Credit/debit cards | grouped digits (4-4-4-4, 4-6-5) that pass the Luhn checksum |
| `routing_number` | Bank routing numbers | labeled, or bare 9 digits passing the ABA checksum |
| `drivers_license` | Driver's license numbers | labeled ("Driver's License: …", "DL #", "License No") |
| `passport` | Passport numbers | labeled ("Passport No: …") |
| `mrn` | Medical record / patient numbers | labeled ("MRN:", "Patient ID:") |
| `insurance_id` | Insurance / member / Medicare / Medicaid IDs | labeled |
| `dob` | Dates of birth | ONLY dates next to a DOB/Date-of-Birth label — other dates are kept |
| `address` | Street addresses, City/ST ZIP lines, PO boxes | pattern |
| `case_number` | Legal case / docket numbers | labeled ("Case No. …") |

Labeled detectors deliberately require the label so that balances, prices,
quantities, and ordinary dates are never redacted.

### `custom_terms`

Exact text removed from every PDF — this is the ONLY way names are
redacted (the tool never guesses names, by design):

```yaml
custom_terms:
  - "John Smith"
  - "Smith, John"      # add each variation your documents use
  - "Dr. Jane Chen"
```

You may also organize terms into named groups — the group names are just
labels for you and both shapes behave identically:

```yaml
custom_terms:
  names:
    - "John Smith"
  addresses:
    - "123 Maple Ave"
```

Matching is case-insensitive, whole-word, non-fuzzy, and tolerant of
whitespace differences ("Jane Smith" matches even when a PDF splits it
across a line break). Terms shorter than 2 characters are reported and
ignored; a `custom_terms` section that yields no usable terms is a hard
error. Reported under "Custom terms (from config)".

### `exclude_terms`

The never-redact allowlist. Any automatic match that contains one of these
strings is left alone — use it for recurring false positives:

```yaml
exclude_terms:
  - "800-555-0199"          # a public support number
  - "support@acmebank.com"  # a public email
```

Honored during verification too, so an excluded term can't fail the
post-redaction check.

### `options`

```yaml
options:
  ocr: true              # default: true
  redact_barcodes: true  # default: true
  office_converter: auto  # auto (default) | simple | msoffice | libreoffice
  handwriting_ocr: false     # default: false
  redact_handwriting: false  # default: false
  redact_faces: false        # default: false
  output:
    images: original     # original (default) | png | pdf
    everything: original # original (default) | pdf
    combine: false        # default: false
```

- `output.images` — what redacted images come back as: `original` keeps
  the input format (HEIC/RAW/PSD can't be written and fall back to JPEG
  with a report note), `png` standardizes everything to lossless PNG,
  `pdf` wraps the redacted image in a one-page PDF. All image outputs are
  rebuilt from pixels — EXIF/GPS/XMP metadata never survives.
- `output.everything` — `pdf` forces every native-format output
  (text/CSV/TSV/XLSX, and images unless `output.images` says otherwise)
  to come back as PDF too, instead of its own format. Default `original`
  leaves formats as they are. The report notes the forced conversion.
- `output.combine` — `true` ALSO writes `output/combined_redacted.pdf`,
  merging every redacted output from the run (in filename order) into
  one PDF, with a table of contents and its own independent
  whole-document re-verification. Individual outputs are still written —
  this is an extra artifact, never a replacement. Same as `run.sh`'s
  `--combine` flag. Refuses (and tells you which file) if any individual
  output's verification didn't pass — a combined artifact must be
  shareable as a unit.
- Document outputs are otherwise fixed by design: Word/PowerPoint/email/
  EPUB/legacy-Office always return PDF (native .docx out would leak
  tracked changes/comments; the others have no safe native write-back).

- `ocr` — read scanned/image-only pages with OCR to locate sensitive text,
  then blank those image regions. Needs Tesseract language data
  (`brew install tesseract`; `run.sh` installs it automatically). With
  `ocr: false` (or Tesseract absent), unreadable pages are reported and the
  run exits with code 2 — never silently skipped.
- `redact_barcodes` — blank QR codes/barcodes that can be decoded inside
  page images (uses zxing-cpp, installed automatically with the other
  Python dependencies). Images where nothing could be decoded are flagged
  in the report for manual review either way.
- `office_converter` — which converter handles Word/PowerPoint/Excel:
  - `auto` (default): LibreOffice if it's installed, else the built-in
    pure-Python converter for `.docx`/`.pptx` (simplified layout, but no
    extra install). `.xls`/`.ppt`/`.odp` have no pure-Python reader at
    all, so they always need LibreOffice — `run.sh` offers to install it
    (Homebrew, free, ~700 MB, fully offline) the first time one appears
    in `input/`, and remembers if you decline.
  - `simple` — force the built-in converter; fails for `.xls`/`.ppt`/`.odp`.
  - `libreoffice` — force LibreOffice for everything, including
    `.docx`/`.pptx` (higher layout fidelity, not simplified, but needs
    LibreOffice installed).
  - `msoffice` — not available. This build's installed Word/Excel/
    PowerPoint have no working AppleScript PDF export (verified by
    testing); selecting this errors with a clear message rather than
    silently falling back.
- `handwriting_ocr` / `redact_handwriting` / `redact_faces` — Apple
  Vision (on-device, no network) for images and scanned PDF pages. All
  default `false`; opt in deliberately.
  - `handwriting_ocr`: recognize handwritten/printed text and redact only
    what matches your categories/`custom_terms` (same rules as
    everywhere else, including `exclude_terms`).
  - `redact_handwriting`: blanket — black out EVERY handwriting-shaped
    region, matched or not. There is no reliable signature-specific
    detector, so this is the mechanism that actually catches signatures;
    it also catches ordinary handwritten notes.
  - `redact_faces`: black out detected face rectangles with solid boxes
    (not blurs — boxes aren't reversible, blurs can be). Best-effort:
    Vision misses heavily occluded/profile/low-resolution faces. Always
    skim the output.

### `passwords` (top-level, not under `options`)

Batch decryption map for password-protected PDFs/`.docx`/`.xlsx`/`.pptx`:

```yaml
passwords:
  statement.pdf: "the-password"
  contract.docx: "another-password"
```

Keyed by filename (as it appears in `input/`). A one-off `--password`
flag beats this map. Wrong or missing password → exit code 5, its own
batch-summary bucket, never a silent skip. This file is already
gitignored (like the rest of `config/redact_config.yaml`) — it's still
your responsibility to keep it off shared machines. Passwords are never
printed by `--check-config` (only a count) or written into any report.

### `custom_patterns` (advanced)

Your own regular expressions, each with a name:

```yaml
custom_patterns:
  - name: employee-id
    regex: '\bEMP-\d{6}\b'
```

Invalid regexes are skipped with a warning at startup.

## Command-line options

Batch (`./scripts/run.sh`):

```
./scripts/run.sh [preset] [extra flags passed to every file]
```

The first argument, if not starting with `-`, is the preset. Everything
else is passed through to `src/redact.py` for each PDF (e.g. `--dry-run`,
`--config other.yaml`).

Single file (`./scripts/redact.sh`, same as `src/redact.py`):

| Flag | Meaning |
|---|---|
| `input.pdf` | The file to redact — any supported format (originals are never modified) |
| `-p / --preset NAME` | Document-type preset; beats the config's `preset` |
| `-o / --output PATH` | Output path (default `<input>_redacted.<ext>`) |
| `-c / --config PATH` | Use another config file for this run. A missing explicit config is a hard error. |
| `-n / --dry-run` | Preview: write only the report (with full unmasked matches), no output file |
| `--categories a,b,c` | Exact category list — bypasses both the preset and the config's `categories` switches |
| `--report PATH` | Report path (default `<output-filename>_report.txt`, e.g. `foo_redacted.pdf_report.txt` — the full output filename, extension included, so two same-stem outputs in different formats never collide) |
| `--password TEXT` | Password for an encrypted PDF/docx/xlsx/pptx (one-off; batch runs can use the config's `passwords:` map instead) |
| `--list-categories` | Print all categories and presets, then exit |
| `--check-config` | Print exactly what the config resolves to — every custom term, switch, and option — then exit. Use it to audit that your terms actually loaded. |

`scripts/run.sh` also accepts `--combine` (see `output.combine` above).

Exit codes: `0` redacted + verified · `2` unreadable scanned page(s)
remain unredacted · `3` post-redaction verification failed (do not share
the output) · `4` unsupported file type · `5` password-protected, could
not open (missing/wrong password).

Unknown config keys (e.g. a typo like `custom_term:`) print a
`! Config:` warning at startup — settings are never silently ignored.

## Master supported-format list

| Family | Extensions | Path | Output |
|---|---|---|---|
| PDF | `.pdf` (+ PDF-compatible `.ai`), incl. password-protected | native pipeline | `.pdf` |
| Word | `.docx` | Tier-1 (pure Python) or LibreOffice | `.pdf` |
| Word legacy | `.doc`, `.odt`, `.rtf` | macOS `textutil` → `.docx` → Tier-1 | `.pdf` |
| PowerPoint | `.pptx` | Tier-1 or LibreOffice | `.pdf` |
| PowerPoint legacy | `.ppt`, `.odp` | LibreOffice (consent-gated install) | `.pdf` |
| Excel | `.xlsx`, incl. password-protected | native (values-only) | `.xlsx` |
| Excel legacy | `.xls` | LibreOffice (consent-gated install) | `.pdf` |
| Spreadsheet text | `.csv`, `.tsv` | native, header-as-context | same |
| Text | `.txt`, `.md`, `.log`, `.json`, `.yaml`, `.yml`, `.xml`, `.html`, `.htm` | native | same |
| Images | `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`, `.tif`, `.tiff`, `.heic`, `.heif`, `.avif`, `.ico` | OCR pipeline (+ optional Vision) | same by default |
| Photoshop / RAW | `.psd`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng` | decode → OCR pipeline | `.jpg`/`.png` |
| Email | `.eml`, `.msg`† | body → PDF, attachments recursed & redacted separately | `.pdf` (+ per-attachment) |
| Ebook | `.epub` (non-DRM) | chapters → Tier-1 renderer → PDF | `.pdf` |
| Encrypted | password-protected PDF/Office (any of the above) | `--password` or config `passwords:` map | per underlying type |

† `.msg` is implemented against `extract-msg`'s documented API but has no
automated test fixture (no pure-Python `.msg` writer exists to build one)
— verify against a real file before relying on it for anything sensitive.

Not possible, told straight: iWork (`.pages`/`.numbers`/`.key` — export
first), DRM-protected ebooks/Office files (DRM is never broken),
signature-specific detection (no such local capability exists —
`redact_handwriting`'s blanket mode is the honest substitute), and MS
Office AppleScript automation (`office_converter: msoffice` — this
Word/Excel/PowerPoint build has no working AppleScript PDF export,
verified by testing; LibreOffice is the fidelity path instead).

## Precedence summary

1. `--categories` (exact list, ignores everything else)
2. `--preset` on the command line
3. `preset:` in the config
4. built-in default `general`

…then the config's `categories:` switches adjust the preset's list
(step 1 skips them), `custom_terms` are always added, and `exclude_terms`
always filter.
