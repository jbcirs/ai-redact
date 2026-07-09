# ai-redact

**Blacks out your personal information in PDFs — permanently — before you
share them with AI tools.**

Drop in a bank statement, medical record, or legal document and get back a
copy with SSNs, account numbers, emails, phones, addresses, and your own
custom terms truly removed (deleted from the file, not just covered up),
plus a report proving it. Financial numbers you care about — balances,
holdings, prices, gains — are left untouched.

Everything runs on your Mac. Nothing is ever uploaded anywhere.

## How to use it

1. **Drop your PDFs into the `input/` folder.**
2. **Run this in Terminal from the project folder:**

   ```bash
   ./scripts/run.sh
   ```

3. **Copy your redacted PDFs out of the `output/` folder.**
   Each one comes with a `_report.txt` — check it says
   `POST-REDACTION VERIFICATION: PASS`, then give the PDF a quick skim.

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
