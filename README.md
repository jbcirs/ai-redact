# ai-redact

**Blacks out your personal information in documents — permanently — before
you share them with AI tools.**

Drop in a bank statement, medical record, Word doc, spreadsheet, photo of
a form, or legal document and get back a copy with SSNs, account numbers,
emails, phones, addresses, and your own custom terms truly removed
(deleted from the file, not just covered up), plus a report proving it.
Financial numbers you care about — balances, holdings, prices, gains —
are left untouched.

Everything runs on your Mac. Nothing is ever uploaded anywhere.

## Supported file types

| Type | Extensions | Comes back as |
|---|---|---|
| PDF | `.pdf` (incl. password-protected — see below) | PDF |
| Word | `.docx`, legacy `.doc`, `.odt`, `.rtf` | PDF (Word files can hide tracked changes/comments — PDF avoids leaking them) |
| PowerPoint | `.pptx` | PDF |
| Legacy Word/PowerPoint/Excel | `.doc`/`.odt`/`.rtf` (built-in, zero extra install), `.ppt`/`.odp`/`.xls` (needs free LibreOffice — you'll be prompted to install it once) | PDF |
| Excel | `.xlsx` (incl. password-protected) | Excel (values only — formulas are dropped so a redacted cell can't be reconstructed) |
| Spreadsheet text | `.csv`, `.tsv` | same format |
| Text | `.txt`, `.md`, `.log`, `.json`, `.yaml`, `.yml`, `.xml`, `.html`, `.htm` | same format |
| Email | `.eml`, `.msg` | PDF (attachments are redacted too, as their own files) |
| Ebook | `.epub` (not DRM-protected) | PDF |
| Photos & images | `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`, `.tif`, `.tiff`, `.heic`, `.heif`, `.avif`, `.ico` | same format by default (GPS/location and all other hidden metadata always stripped) |
| Camera RAW & Photoshop | `.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, `.psd` | JPEG or PNG (these formats can't be written back to their original form) |

Scanned pages and photographed documents are read with on-device OCR
before redacting. Optional (off by default): recognize/redact handwriting
and black out faces in images/scanned pages using Apple's on-device
Vision framework — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Password-protected PDFs/Word/Excel/PowerPoint files are supported —
either pass `--password` for a one-off, or add a `passwords:` map to your
config for batch runs. Wrong/missing password never fails silently.

**Not yet supported:** Apple/iWork files (`.pages`, `.numbers`, `.key` —
export them to PDF or Word first) and DRM-protected ebooks (DRM is never
broken). Anything else lands in an "Unsupported" bucket in the batch
summary rather than being guessed at.

## How to use it

1. **Drop your documents into the `input/` folder** (any supported type,
   mixed together is fine).
2. **Run this in Terminal from the project folder:**

   ```bash
   ./scripts/run.sh
   ```

3. **Copy your redacted files out of the `output/` folder.**
   Each one has a matching report in `output/logs/` — check it says
   `POST-REDACTION VERIFICATION: PASS`, then give the file a quick skim.

Text, spreadsheets, CSVs, and images come back in their own format
(images with all hidden metadata stripped); Word and PowerPoint come back
as PDFs — that's deliberate, because Word files can hide tracked changes
and comments that "redacted" copies would leak.

That's it. The first run sets everything up automatically (takes a
minute). `output/` is emptied at the start of every run, and your files in
`input/` are never touched.

## Make it yours: the config file

Open **`config/redact_config.yaml`** (created on first run) in any text
editor. This is where you:

- add **your names, kids' names, doctors, businesses** — names are only
  redacted if you list them here
- turn detectors **on/off** (emails, phones, SSNs, driver's licenses,
  passports, and credit cards are always-on out of the box)
- set your default document type: `financial`, `medical`, `legal`, or
  `general`

The file is fully commented, stays on your Mac, and can't be committed to
git. Every setting is explained in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Want more detail?

| Doc | What's in it |
|---|---|
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Step-by-step usage, dry-run preview, single-file mode, testing with fake data, troubleshooting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every config setting and command-line option |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works inside |

## The honest fine print

- Automated redaction is a **first pass, not a guarantee** — always skim
  the output before sharing.
- Scanned/photographed pages are read with OCR; pages that can't be read
  are reported loudly, never silently skipped.
- Names are never guessed — they come only from your config list.
