# Handler Contract Spec (for format-support execution)

Binding contract for `src/handlers/*.py` modules. The router in
`src/redact.py` dispatches by sniffed file type. Two handler kinds exist.
Handlers must be import-safe on Python 3.13 with only the packages in
requirements.txt; optional deps (rawpy, pillow_heif) are imported lazily
inside functions with a clear `UnsupportedFormatError` on failure.

## Shared rules (all handlers)

- **Local only.** No network. No subprocesses except explicitly allowed.
- **Fail loud.** Raise `handlers.common.UnsupportedFormatError(message)`
  with a user-actionable message rather than degrading silently.
- **Never modify the input file.**
- Import shared helpers from `handlers.common` (provided by the lead):

```python
class UnsupportedFormatError(Exception): ...

# Matcher callable passed in by the router (wraps detection + exclude):
#   matcher(text: str) -> list[tuple[str, str]]   # (category, matched_string)
```

## Kind A — convert handlers (module exposes `to_pdf`)

For formats redacted THROUGH the existing PDF pipeline (images, docx,
pptx). The handler only converts; the router runs the proven PDF
redaction/verify afterwards.

```python
SUPPORTED_EXTENSIONS: set[str]   # e.g. {".docx"}

def to_pdf(input_path: pathlib.Path, options: dict) -> tuple[bytes, dict]:
    """Convert input to PDF bytes.

    options: the config's options: mapping (may be empty).
    Returns (pdf_bytes, info) where info = {
        "converter": "tier1-python-docx",       # short id for the report
        "dropped_elements": 0,                   # count of content the
                                                 # converter could NOT carry
                                                 # over (safe direction, but
                                                 # MUST be counted)
        "notes": ["speaker notes included"],    # list[str] for the report
    }
    """
```

Image handlers additionally expose write-back so `output.images: original`
works:

```python
def write_back(pdf_doc, input_path, output_path, options) -> pathlib.Path:
    """Render the redacted 1-page PDF back to the original raster format
    (or PNG when the original can't be written). MUST strip all metadata
    (EXIF/GPS/XMP). Returns the actual output path written (extension may
    differ from requested when the original format is unwritable)."""
```

## Kind B — native handlers (module exposes `redact_file`)

For formats redacted in their own format (text, csv, xlsx).

```python
SUPPORTED_EXTENSIONS: set[str]

def redact_file(input_path, output_path, matcher, dry_run: bool,
                options: dict) -> dict:
    """Scan (and unless dry_run, write redacted output). Returns:
    {
      "counts": {category: int},          # matches per category
      "matches": [(unit, category, matched_text, True)],
                                          # unit = page/sheet/line label str
      "unit_label": "line",               # "line" | "sheet" | "cell" ...
      "unit_count": 240,                   # total units scanned
      "notes": [str],                      # handler-specific report notes
    }
    Replacement text for redacted spans: "█" * min(len(match), 12).
    """

def verify_file(output_path, matcher, options: dict) -> dict:
    """Re-open the WRITTEN output, re-run matcher over ALL extracted
    content. Returns {category: remaining_count} — MUST be {} on success.
    This is mandatory; a native handler without verification is rejected."""
```

## Ownership map (one module per agent — do not touch other files)

| Agent | Files owned |
|---|---|
| worker-text | `src/handlers/text_handler.py`, `src/handlers/csv_handler.py`, `tests/make_text_fixtures.py` |
| worker-image | `src/handlers/image_handler.py`, `tests/make_image_fixtures.py` |
| worker-office | `src/handlers/office_handler.py`, `src/handlers/excel_handler.py`, `tests/make_office_fixtures.py` |
| lead | `src/handlers/__init__.py`, `src/handlers/common.py`, router, CLI, config, run.sh, test.sh, docs |

## Fixture generators (`tests/make_*_fixtures.py`)

Each generator writes fake-PII files into a directory given as argv[1].
Every fixture must plant, where the format allows: an email
(`planted.email@example.com`), a phone (`(555) 010-9999`), an SSN
(`000-55-4444`), a custom-term name (`Casey Plantedname`), and — for
financial-preservation checks — the exact string `$12,345.67` which must
SURVIVE redaction. Print one line per file written. Pure Python, no
network, deps from requirements.txt only.

## Standalone self-test

Each handler module must run a smoke test when executed directly
(`python -m` or direct): generate one fixture in a temp dir, run its own
convert/redact path with a trivial matcher (regex for the planted email),
and assert the planted email is gone from the result (for Kind A:
assert the produced PDF's text contains the planted email BEFORE
redaction — conversion only). Exit 0 on success, non-zero with a message
on failure.
