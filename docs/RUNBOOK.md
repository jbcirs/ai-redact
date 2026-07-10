# Runbook — ai-redact

Step-by-step operating guide. For how the tool works internally, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Normal operation

1. **Move anything you want to keep out of `output/`** — it is emptied at
   the start of every run.
2. Drop the documents to redact into `input/` (Finder is fine). Supported:
   PDF (incl. password-protected), Word/PowerPoint (.docx/.pptx, plus
   legacy .doc/.odt/.rtf built-in and .ppt/.odp/.xls via LibreOffice),
   Excel (.xlsx, incl. password-protected), CSV/TSV,
   text/HTML/JSON/YAML/XML, email (.eml/.msg — attachments redacted too),
   EPUB, and images (jpg, png, tiff, heic, webp, gif, avif, bmp, ico, psd,
   camera RAW). Anything else (iWork files, DRM'd ebooks) lands in an
   "Unsupported" bucket in the summary — nothing is guessed at.
3. From the project folder, run:

   ```bash
   ./scripts/run.sh financial       # or: medical, legal, general (default)
   ```

4. Read the **BATCH SUMMARY** at the end. For every file you should see
   `Verify : PASS`.
5. Open each `output/*_report.txt` and confirm:
   - `POST-REDACTION VERIFICATION` says **PASS**
   - the `WARNINGS` section is empty (or you've manually handled each one)
6. Skim each `output/*_redacted.pdf` with your own eyes before uploading it
   anywhere. Automated redaction is a first pass, not a guarantee.

The first run sets everything up automatically: creates `.venv/`, installs
PyMuPDF/PyYAML, and installs Tesseract OCR via Homebrew if it's missing.

## Preview before committing (dry run)

```bash
./scripts/run.sh financial --dry-run
```

Writes only reports to `output/` (no PDFs). Dry-run reports show the FULL
matched values so you can check accuracy; real-run reports mask them.

## Single file (without the batch flow)

```bash
./scripts/redact.sh path/to/file.pdf --preset medical
./scripts/redact.sh path/to/file.pdf --categories email,phone -o custom_name.pdf
./scripts/redact.sh protected.pdf --password "the-password"
./scripts/redact.sh --list-categories
```

## Combining everything into one PDF

```bash
./scripts/run.sh financial --combine
```

Writes `output/combined_redacted.pdf` (all of this run's outputs merged,
in filename order, with a table of contents) ALONGSIDE the individual
outputs — never instead of them. Refuses (prints which file, exits
non-zero) if any individual output didn't pass verification; the
combined PDF is also independently re-verified as a whole. Same as
setting `output.combine: true` in the config.

## The config file

`config/redact_config.yaml` is auto-created from the template on first run
and used by default on every run. Everything it can do is defined in
[CONFIGURATION.md](CONFIGURATION.md); the highlights:

- `custom_terms` — exact text to always remove: your names, kids' names,
  doctor names, businesses, odd account formats. Add every variation that
  appears in your documents. **This is the only way names get redacted.**
- `categories` — force any detector on (`true`) or off (`false`) for every
  run, regardless of preset. Emails, phones, SSNs, driver's licenses,
  passports, and credit cards ship turned on.
- `preset` — your default document type when the command line doesn't say.
- `exclude_terms` — never-redact list for recurring false positives (e.g.
  a company's public 800 number).
- `options` — toggles for OCR, QR/barcode blanking, which Office
  converter to use, everything-to-PDF/combine, and Apple Vision
  handwriting/face redaction (all opt-in, off by default).
- `passwords` — filename → password map for encrypted PDFs/Office files
  (batch runs); `--password` covers one-off single files.

To run with a different config: `--config other.yaml` (works for both
`./scripts/redact.sh` and `./scripts/run.sh <preset> --config other.yaml`).
A missing `--config` file is a hard error, never silently ignored.

## Testing with fake data

```bash
.venv/bin/python src/make_sample_pdf.py    # writes 2 fake PDFs into input/
./scripts/run.sh financial
open output/sample_statement_redacted.pdf
open output/sample_scanned_redacted.pdf    # exercises the OCR path
```

## Regression test suite (every format)

```bash
./scripts/test.sh
```

Generates fake fixtures for every supported format (each planting known
identifiers plus a `$12,345.67` string that must SURVIVE), redacts them
all in a temp dir, and asserts the planted PII is gone from every output
— including by re-OCR'ing redacted images — and the financial string
remains. Run it after any change to detection patterns or handlers.

## Auditing your config

```bash
./scripts/redact.sh --check-config
```

Prints every custom term, category switch, and option exactly as the tool
resolved them — if a name you added doesn't appear in the list, it isn't
being redacted (check the YAML structure).

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `VERIFY FAILED` in batch summary / `Verify : *** FAIL` / exit code 3 | Sensitive text still detectable in the output | **Do not share the file.** Open the report to see which category remains; usually text split across lines — redact those spots manually (Preview's redact tool) or add the exact string to `custom_terms`. |
| `Unreadable` in batch summary / exit code 2 | Page images couldn't be read even with OCR (or OCR isn't installed) | Install OCR (`brew install tesseract`) and re-run; if it persists, pre-process with `ocrmypdf <in> <out>` or redact manually. |
| `<< NOT LOCATED` in report | A match was found in the text but its position couldn't be pinned down | That item was NOT redacted (verification will fail too). Fix manually on the listed page. |
| `IMAGES PRESENT on pages …` warning | Pages contain pictures in which no barcode was detected — they may still hold QR codes, signatures, or scanned IDs | Look at those pages yourself. (Decodable QR codes/barcodes are blanked automatically.) |
| A name/ID slipped through | Not in any pattern and not in your custom list | Add the exact text to `custom_terms` in `config/redact_config.yaml`, re-run. |
| Something was redacted that shouldn't be | Pattern false positive | Add the exact text to `exclude_terms` in `config/redact_config.yaml`, or drop the offending category via `--categories`. |
| `EMBEDDED FILE ATTACHMENTS` in report | The PDF carried file attachments (invisible on the page) | They were removed automatically — nothing to do, just be aware the original had them. |
| `Encrypted` in batch summary / exit code 5 | File is password-protected and couldn't be opened | Pass `--password`, or add the file to the config's `passwords:` map, and re-run. |
| `Unsupported` for `.xls`/`.ppt`/`.odp` | LibreOffice isn't installed and you declined the prompt | Re-run interactively and accept, or `brew install --cask libreoffice` yourself, or export the file as .docx/.pptx/.xlsx. |
| `office_converter: msoffice` errors immediately | MS Office AppleScript automation doesn't work on this machine (verified) | Use `office_converter: libreoffice` or `simple` instead — this isn't a config mistake, see docs/CONFIGURATION.md. |
| `Setup has not been run yet` from `./scripts/redact.sh` | No `.venv` | Run `./scripts/run.sh` once, or follow the manual setup in the README. |

Exit codes (single-file `./scripts/redact.sh`): `0` fully redacted + verified, `2`
unreadable page(s) remain, `3` verification failed, `4` unsupported file
type, `5` password-protected/could not open. `scripts/run.sh` sorts files
into the summary buckets using these.

## What can still be inferred from a redacted PDF

The redacted values themselves are unrecoverable — the text is deleted
from the file, not hidden under the boxes (verified on every run). But be
aware of what remains:

- **Categories**: labels are kept, so `SSN: ███` reveals *that* an SSN was
  there (not what it was).
- **Lengths**: a black box is as wide as the text it replaced — a name's
  approximate length is visible.
- **Context**: whatever you DON'T redact (clinic names, employers, dates,
  unusual holdings) can be combined to narrow down who the document is
  about. Add identifying context to `custom_terms` if that matters to you.
- **Never upload the `_report.txt`** alongside the PDF — masked values
  still show first/last characters.

## Rules of thumb

- `input/` files are never modified; originals are always safe.
- `output/` is disposable — anything there is regenerated by the next run.
- The verification PASS means the *patterns* find nothing in the output.
  It cannot prove a human wouldn't spot something the patterns don't know
  about — hence the final skim.
