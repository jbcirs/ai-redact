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

Matching is case-insensitive, whole-word, non-fuzzy. Terms shorter than 2
characters are ignored. Reported under "Custom terms (from config)".

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
```

- `ocr` — read scanned/image-only pages with OCR to locate sensitive text,
  then blank those image regions. Needs Tesseract language data
  (`brew install tesseract`; `run.sh` installs it automatically). With
  `ocr: false` (or Tesseract absent), unreadable pages are reported and the
  run exits with code 2 — never silently skipped.
- `redact_barcodes` — blank QR codes/barcodes that can be decoded inside
  page images (uses zxing-cpp, installed automatically with the other
  Python dependencies). Images where nothing could be decoded are flagged
  in the report for manual review either way.

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
| `input.pdf` | The PDF to redact (originals are never modified) |
| `-p / --preset NAME` | Document-type preset; beats the config's `preset` |
| `-o / --output PATH` | Output PDF (default `<input>_redacted.pdf`) |
| `-c / --config PATH` | Use another config file for this run. A missing explicit config is a hard error. |
| `-n / --dry-run` | Preview: write only the report (with full unmasked matches), no PDF |
| `--categories a,b,c` | Exact category list — bypasses both the preset and the config's `categories` switches |
| `--report PATH` | Report path (default `<output>_report.txt`) |
| `--list-categories` | Print all categories and presets, then exit |

Exit codes: `0` redacted + verified · `2` unreadable scanned page(s)
remain unredacted · `3` post-redaction verification failed (do not share
the output).

## Precedence summary

1. `--categories` (exact list, ignores everything else)
2. `--preset` on the command line
3. `preset:` in the config
4. built-in default `general`

…then the config's `categories:` switches adjust the preset's list
(step 1 skips them), `custom_terms` are always added, and `exclude_terms`
always filter.
