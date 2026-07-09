#!/usr/bin/env python3
"""
redact.py — Local PDF redaction tool for macOS.

Permanently removes sensitive identifiers from searchable PDFs before you
share them with AI tools or anyone else. Runs 100% locally — no network
access, nothing is uploaded anywhere.

Outputs:
  1. A redacted PDF (text is truly deleted, not just covered with boxes).
  2. A verification report listing what was redacted, per category, and the
     result of a post-redaction re-scan of the output file.

Usage examples:
  python3 redact.py statement.pdf --preset financial
  python3 redact.py labs.pdf --preset medical --dry-run
  python3 redact.py doc.pdf --config redact_config.yaml -o clean.pdf

See README.md for full setup and usage instructions.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit(
        "PyMuPDF is not installed.\n"
        "Run:  pip install -r requirements.txt\n"
        "(See README.md for macOS setup instructions.)"
    )

try:
    import yaml
except ImportError:
    yaml = None  # config file support degrades gracefully

def find_tessdata():
    """Locate Tesseract language data so PyMuPDF can OCR scanned pages.

    Returns a tessdata directory path, or None if OCR is unavailable.
    PyMuPDF has Tesseract built in — it only needs the language data files
    (installed on macOS via 'brew install tesseract').
    """
    import os

    p = os.environ.get("TESSDATA_PREFIX")
    if p and os.path.isdir(p):
        return p
    try:
        return fitz.get_tessdata()  # finds an installed tesseract's data
    except Exception:
        pass
    for cand in ("/opt/homebrew/share/tessdata", "/usr/local/share/tessdata"):
        if os.path.isdir(cand):
            return cand
    return None


TESSDATA = find_tessdata()

# Project root (this file lives in src/). The default config is used on
# every run unless --config points somewhere else.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = "config/redact_config.yaml"

# pyzbar is optional: if installed (with the zbar system library), QR codes
# and barcodes inside page images are decoded and redacted. Without it, pages
# containing images are only flagged for manual review.
try:
    from pyzbar.pyzbar import decode as zbar_decode
    from PIL import Image
    import io

    HAVE_ZBAR = True
except Exception:
    HAVE_ZBAR = False


# ---------------------------------------------------------------------------
# Built-in detection patterns
# ---------------------------------------------------------------------------
# Each pattern is a dict:
#   regex     - compiled regular expression
#   group     - optional named group to redact ("redact"); if absent, the
#               whole match is redacted. Lets us match "DOB: 01/02/1980"
#               on context but only black out the date itself.
#   validator - optional function(matched_text) -> bool for extra checks
#               (e.g. ABA routing-number checksum) to cut false positives.
#
# Design rule: numeric patterns are CONTEXTUAL wherever possible (they require
# a nearby label like "Account #" or "MRN:") so that balances, share counts,
# prices, and dates are NOT redacted.

MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE = r"(?:\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|" + MONTHS + r"\.?\s+\d{1,2},?\s+\d{4})"


def _aba_checksum_ok(digits: str) -> bool:
    """Validate a 9-digit ABA routing number checksum."""
    d = [int(c) for c in digits if c.isdigit()]
    if len(d) != 9:
        return False
    total = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return total % 10 == 0 and total > 0


def _mostly_digits(text: str, minimum: int = 5) -> bool:
    """Require at least `minimum` digits — filters out short/label-like hits."""
    return sum(c.isdigit() for c in text) >= minimum


def _luhn_ok(text: str) -> bool:
    """Luhn checksum — true for real card numbers, rarely for other digits."""
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Label-to-value separator for contextual patterns. Deliberately does NOT
# cross line breaks: a label at the end of one line must not capture whatever
# word starts the next line (e.g. the next field's label).
SEP = r"[ \t]*[:#.\-]?[ \t]*"


def _p(regex: str, flags=re.IGNORECASE, group: str = None, validator=None):
    return {"regex": re.compile(regex, flags), "group": group, "validator": validator}


CATEGORY_PATTERNS = {
    "email": [
        _p(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", flags=0),
    ],
    "phone": [
        # Requires separators (dashes/dots/spaces/parens) so plain digit runs
        # like account numbers or quantities are not mistaken for phones.
        _p(r"(?<![\d.\-])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d.\-])", flags=0),
    ],
    "ssn": [
        _p(r"\b\d{3}-\d{2}-\d{4}\b", flags=0),
        # Unformatted 9 digits, but only next to an SSN label.
        _p(r"(?:SSN|Soc(?:ial)?\.?[ \t]*Sec(?:urity)?\.?(?:[ \t]*(?:No|Num(?:ber)?|#))?)" + SEP +
           r"(?P<redact>\d{3}[ \-]?\d{2}[ \-]?\d{4})", group="redact"),
    ],
    "tax_id": [
        _p(r"\b\d{2}-\d{7}\b", flags=0),  # EIN format
        _p(r"(?:EIN|TIN|ITIN|Tax(?:payer)?[ \t]*ID(?:[ \t]*(?:No|Num(?:ber)?|#))?)" + SEP +
           r"(?P<redact>[\d\-]{9,11})",
           group="redact", validator=lambda t: _mostly_digits(t, 9)),
    ],
    "account_number": [
        # Labeled: "Account #: 1234-567890", "Acct No. Z98765432"
        _p(r"(?:account|acct|a/c)[ \t]*(?:no|num(?:ber)?|#)?" + SEP +
           r"(?P<redact>[A-Z0-9][A-Z0-9\-]{4,24}\d)",
           group="redact", validator=lambda t: _mostly_digits(t, 5)),
        # Masked forms: "XXXX-1234", "****6789", "•••• 4321"
        _p(r"(?:[Xx*•]{2,}[\s\-]?){1,4}\d{2,6}\b", flags=0),
        # Standalone long digit runs (8–17 digits, not part of a money amount
        # or decimal). Long enough that balances/prices never match.
        _p(r"(?<![\d.,$\-])\d{8,17}(?![\d.,])", flags=0),
    ],
    "credit_card": [
        # 16-digit cards in 4-4-4-4 groups and Amex 4-6-5, with separators.
        # (Unseparated runs are already caught by account_number.) The Luhn
        # checksum keeps other grouped numbers from matching.
        _p(r"\b\d{4}[ \-]\d{4}[ \-]\d{4}[ \-]\d{4}\b", flags=0, validator=_luhn_ok),
        _p(r"\b\d{4}[ \-]\d{6}[ \-]\d{5}\b", flags=0, validator=_luhn_ok),
    ],
    "routing_number": [
        _p(r"(?:routing|ABA|RTN)[ \t]*(?:no|num(?:ber)?|#)?" + SEP + r"(?P<redact>\d{9})\b",
           group="redact"),
        # Bare 9-digit numbers only when the ABA checksum passes.
        _p(r"\b\d{9}\b", flags=0, validator=_aba_checksum_ok),
    ],
    "mrn": [
        _p(r"(?:MRN|Med(?:ical)?\.?[ \t]*Rec(?:ord)?\.?[ \t]*(?:No|Num(?:ber)?|#)?|Patient[ \t]*(?:ID|No|Num(?:ber)?|#))" + SEP +
           r"(?P<redact>[A-Z0-9\-]{4,15})",
           group="redact", validator=lambda t: _mostly_digits(t, 2)),
    ],
    "insurance_id": [
        _p(r"(?:Member|Policy|Subscriber|Group|Insurance|Plan)[ \t]*(?:ID|No|Num(?:ber)?|#)" + SEP +
           r"(?P<redact>[A-Z0-9\-]{5,20})",
           group="redact", validator=lambda t: _mostly_digits(t, 3)),
        _p(r"(?:Medicare|Medicaid|MBI)[ \t]*(?:ID|No|Num(?:ber)?|#)?" + SEP +
           r"(?P<redact>[A-Z0-9][A-Z0-9\-]{6,15})",
           group="redact", validator=lambda t: _mostly_digits(t, 3)),
    ],
    "drivers_license": [
        # Labeled driver's license numbers (formats vary by state).
        _p(r"(?:Driver'?s?[ \t]*Lic(?:ense)?\.?|D\.?L\.?[ \t]*(?:No|Num(?:ber)?|#)|"
           r"Lic(?:ense)?[ \t]*(?:No|Num(?:ber)?|#))" + SEP +
           r"(?P<redact>[A-Z0-9][A-Z0-9\-]{3,15})",
           group="redact", validator=lambda t: _mostly_digits(t, 3)),
    ],
    "passport": [
        _p(r"Passport[ \t]*(?:No|Num(?:ber)?|#|Book)?" + SEP +
           r"(?P<redact>[A-Z0-9][A-Z0-9\-]{5,12})",
           group="redact", validator=lambda t: _mostly_digits(t, 4)),
    ],
    "dob": [
        # Only dates next to a birth-date label — other dates are preserved.
        _p(r"(?:DOB|D\.O\.B\.?|Date[ \t]*of[ \t]*Birth|Birth[ \t]*Date|Born)" + SEP +
           r"(?P<redact>" + DATE + r")", group="redact"),
    ],
    "address": [
        # Street address: "123 Maple Grove Ave, Apt 4B"
        _p(r"\b\d{1,6}\s+[A-Z][A-Za-z]*(?:\s+[A-Za-z]+){0,3}\s+"
           r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|"
           r"Circle|Cir|Way|Place|Pl|Terrace|Ter|Trail|Trl|Parkway|Pkwy|Highway|Hwy|Loop|Square|Sq)\.?"
           r"(?:[,\s]+(?:Apt|Apartment|Suite|Ste|Unit|Bldg|Floor|Fl|#)\.?\s*[\w\-]+)?",
           flags=0),
        # City, ST 12345[-6789]
        _p(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", flags=0),
        # PO Box
        _p(r"\bP\.?\s?O\.?\s?Box\s+\d+\b"),
    ],
    "case_number": [
        _p(r"(?:Case|Docket|Cause|Matter)[ \t]*(?:No|Num(?:ber)?|#)\.?" + SEP +
           r"(?P<redact>[A-Z0-9][A-Z0-9:\-]{3,24})",
           group="redact", validator=lambda t: _mostly_digits(t, 2)),
    ],
}

# Human-readable names for the report.
CATEGORY_LABELS = {
    "email": "Email addresses",
    "phone": "Phone numbers",
    "ssn": "Social Security numbers",
    "tax_id": "Tax IDs (EIN/TIN)",
    "account_number": "Account numbers",
    "credit_card": "Credit/debit card numbers",
    "routing_number": "Routing numbers",
    "drivers_license": "Driver's license numbers",
    "passport": "Passport numbers",
    "link": "Hyperlinks with sensitive data",
    "mrn": "MRNs / patient IDs",
    "insurance_id": "Insurance / member IDs",
    "dob": "Dates of birth",
    "address": "Addresses",
    "case_number": "Case / docket numbers",
    "custom": "Custom terms (from config)",
}

# ---------------------------------------------------------------------------
# Presets — which categories each document type enables.
# "custom" (terms from the config file) is always enabled.
# ---------------------------------------------------------------------------
PRESETS = {
    "financial": ["account_number", "credit_card", "routing_number", "tax_id",
                  "ssn", "address", "phone", "email", "dob"],
    "medical":   ["mrn", "insurance_id", "credit_card", "dob", "ssn",
                  "address", "phone", "email"],
    "legal":     ["case_number", "drivers_license", "passport", "ssn", "dob",
                  "address", "phone", "email"],
    "general":   list(CATEGORY_PATTERNS.keys()),  # everything
}

PRESET_NOTES = {
    "legal": ("Signatures cannot be reliably auto-detected (they are images, "
              "not text). Pages containing images are flagged in the report "
              "for manual review."),
    "financial": ("Names are only redacted if you list them under "
                  "custom_terms in the config file."),
    "medical": ("Patient/doctor names are only redacted if you list them "
                "under custom_terms in the config file."),
    "general": ("Names are only redacted if you list them under "
                "custom_terms in the config file."),
}


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    """Load the YAML config file (custom terms, custom patterns, options)."""
    if not path.exists():
        return {}
    if yaml is None:
        sys.exit("PyYAML is not installed but a config file was given.\n"
                 "Run:  pip install -r requirements.txt")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        sys.exit(f"Config file {path} is not valid (expected key: value pairs).")
    return cfg


def build_custom_patterns(cfg: dict):
    """Turn config custom_terms / custom_patterns into pattern dicts."""
    patterns = []
    for term in cfg.get("custom_terms") or []:
        term = str(term).strip()
        if len(term) < 2:
            continue  # 1-char terms would redact far too much
        # Whole-word, case-insensitive exact match of the literal term.
        patterns.append(_p(r"(?<!\w)" + re.escape(term) + r"(?!\w)"))
    for item in cfg.get("custom_patterns") or []:
        if isinstance(item, dict) and item.get("regex"):
            try:
                patterns.append(_p(item["regex"]))
            except re.error as e:
                print(f"  ! Skipping invalid custom pattern "
                      f"'{item.get('name', item['regex'])}': {e}")
    return patterns


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def find_matches_in_text(text: str, categories: dict, exclude=()) -> list:
    """Run every enabled pattern over one page's text.

    `exclude` is the config's exclude_terms allowlist: matches equal to (or
    containing) an excluded term are skipped — for recurring false positives
    like a company's public 800 number.

    Returns a list of (category, matched_string) tuples, deduplicated,
    longest matches first (so overlapping shorter hits don't leave
    fragments behind).
    """
    found = {}
    for category, patterns in categories.items():
        for pat in patterns:
            for m in pat["regex"].finditer(text):
                s = m.group(pat["group"]) if pat["group"] else m.group(0)
                s = s.strip()
                if not s:
                    continue
                if pat["validator"] and not pat["validator"](s):
                    continue
                low = s.lower()
                if any(x in low for x in exclude):
                    continue
                found.setdefault((category, s), True)
    return sorted(found.keys(), key=lambda cs: -len(cs[1]))


def locate_on_page(page: "fitz.Page", needle: str, textpage=None) -> list:
    """Find the on-page rectangles for a matched string.

    Normalizes internal whitespace since PDF text extraction may render
    a visual space run differently from the search index. When `textpage`
    is an OCR textpage, the rectangles map to the scanned image regions.
    """
    rects = page.search_for(needle, textpage=textpage)
    if not rects and ("\n" in needle or "  " in needle):
        rects = page.search_for(" ".join(needle.split()), textpage=textpage)
    return rects


def ocr_page(page: "fitz.Page"):
    """OCR a scanned page. Returns (textpage, text) or (None, "")."""
    try:
        tp = page.get_textpage_ocr(dpi=300, full=True, tessdata=TESSDATA)
        return tp, page.get_text("text", textpage=tp)
    except Exception:
        return None, ""


def mask_value(s: str) -> str:
    """Partially mask a matched value for the (post-redaction) report."""
    s = " ".join(s.split())
    if len(s) <= 4:
        return "*" * len(s)
    return s[0] + "*" * (len(s) - 3) + s[-2:]


# ---------------------------------------------------------------------------
# Barcode / QR handling
# ---------------------------------------------------------------------------
def scan_page_images(doc, page):
    """Return (barcode_rects, has_images).

    If pyzbar is available, decode each embedded image and return the page
    rectangles of any barcode/QR found so they can be redacted. Otherwise
    just report whether the page contains images at all (flagged for
    manual review in the report).
    """
    images = page.get_images(full=True)
    if not images:
        return [], False
    if not HAVE_ZBAR:
        return [], True
    barcode_rects = []
    for img in images:
        xref = img[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4:  # CMYK etc. -> RGB
                pix = fitz.Pixmap(fitz.csRGB, pix)
            pil = Image.open(io.BytesIO(pix.tobytes("png")))
            if zbar_decode(pil):
                barcode_rects.extend(page.get_image_rects(xref))
        except Exception:
            continue  # unreadable image; it stays flagged via has_images
    return barcode_rects, True


# ---------------------------------------------------------------------------
# Main redaction pass
# ---------------------------------------------------------------------------
def process_pdf(input_path: Path, output_path: Path, categories: dict,
                dry_run: bool, redact_barcodes: bool, exclude=(),
                use_ocr: bool = True):
    """Scan (and unless dry_run, redact) the PDF. Returns a results dict."""
    doc = fitz.open(input_path)
    ocr_enabled = bool(TESSDATA) and use_ocr

    results = {
        "counts": {},          # category -> total matches
        "matches": [],         # (page_no, category, matched_text, located)
        "scanned_pages": [],   # image-only pages that could NOT be OCR'd
        "ocr_pages": [],       # image-only pages redacted via OCR
        "image_pages": [],     # pages containing images (QR/signature risk)
        "barcode_pages": [],   # pages where a QR/barcode was decoded+redacted
        "unlocated": [],       # matched in text but not found visually
        "embedded_files": [],  # file attachments (removed — may carry PII)
        "toc_redacted": 0,     # bookmark titles containing matches
        "total_text_chars": 0,
        "page_count": len(doc),
        "ocr_enabled": ocr_enabled,
    }

    # File attachments can carry anything; they are invisible on the page,
    # so remove them wholesale rather than trying to scan inside them.
    try:
        results["embedded_files"] = list(doc.embfile_names())
        if not dry_run:
            for name in results["embedded_files"]:
                doc.embfile_del(name)
    except Exception:
        pass

    # Bookmark (table-of-contents) titles can leak names/IDs.
    toc = doc.get_toc(simple=True)
    for entry in toc:
        if find_matches_in_text(str(entry[1]), categories, exclude):
            entry[1] = "[redacted]"
            results["toc_redacted"] += 1
    if results["toc_redacted"] and not dry_run:
        doc.set_toc(toc)

    for page in doc:
        page_no = page.number + 1
        text = page.get_text("text")
        results["total_text_chars"] += len(text.strip())

        barcode_rects, has_images = scan_page_images(doc, page)
        if has_images:
            results["image_pages"].append(page_no)

        # Scanned page (image, no text layer): OCR it so we can find WHERE
        # the sensitive text sits inside the image. The redaction boxes then
        # blank out those image regions permanently.
        textpage = None
        if not text.strip():
            if not has_images:
                continue  # genuinely blank page
            if ocr_enabled:
                textpage, text = ocr_page(page)
            if textpage and text.strip():
                results["ocr_pages"].append(page_no)
                results["total_text_chars"] += len(text.strip())
            else:
                results["scanned_pages"].append(page_no)
                continue  # can't read this page — reported, never faked

        # --- hyperlinks: a mailto:/tel:/URL target can leak data even after
        # the visible text is gone, so matching links are deleted outright ---
        for link in reversed(page.get_links()):
            uri = link.get("uri") or ""
            if uri and find_matches_in_text(uri, categories, exclude):
                results["counts"]["link"] = results["counts"].get("link", 0) + 1
                results["matches"].append((page_no, "link", uri, True))
                if not dry_run:
                    page.delete_link(link)

        # --- text matches ---
        for category, matched in find_matches_in_text(text, categories, exclude):
            rects = locate_on_page(page, matched, textpage)
            located = bool(rects)
            results["counts"][category] = (
                results["counts"].get(category, 0) + max(len(rects), 1))
            results["matches"].append((page_no, category, matched, located))
            if not located:
                results["unlocated"].append((page_no, category, matched))
            if not dry_run:
                for r in rects:
                    page.add_redact_annot(r, fill=(0, 0, 0))

        # --- decoded barcodes/QR codes ---
        if barcode_rects and redact_barcodes:
            results["barcode_pages"].append(page_no)
            results["counts"]["barcode"] = (
                results["counts"].get("barcode", 0) + len(barcode_rects))
            if not dry_run:
                for r in barcode_rects:
                    page.add_redact_annot(r, fill=(0, 0, 0))

    if not dry_run:
        for page in doc:
            # This PERMANENTLY deletes the underlying text beneath each
            # redaction rectangle — not just a black box on top — and blanks
            # the overlapping image pixels (crucial for OCR'd scan pages).
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        # Scrub document metadata (author, title, etc.) as well.
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        doc.save(output_path, garbage=4, deflate=True)

    doc.close()
    return results


def verify_output(output_path: Path, categories: dict, ocr_pages=(),
                  exclude=()) -> dict:
    """Re-open the redacted PDF and re-run every pattern.

    Proves the sensitive text is actually GONE from the file, not hidden.
    Pages that were redacted via OCR are OCR'd AGAIN here, so we verify the
    scanned image itself no longer shows the sensitive text. Link targets
    and bookmark titles are re-checked too.
    Returns {category: remaining_count}.
    """
    doc = fitz.open(output_path)
    remaining = {}

    def count(found):
        for category, _ in found:
            remaining[category] = remaining.get(category, 0) + 1

    for page in doc:
        text = page.get_text("text")
        if not text.strip() and (page.number + 1) in ocr_pages:
            _, text = ocr_page(page)
        count(find_matches_in_text(text, categories, exclude))
        for link in page.get_links():
            uri = link.get("uri") or ""
            if uri and find_matches_in_text(uri, categories, exclude):
                remaining["link"] = remaining.get("link", 0) + 1
    for entry in doc.get_toc(simple=True):
        count(find_matches_in_text(str(entry[1]), categories, exclude))
    doc.close()
    return remaining


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(report_path: Path, input_path: Path, output_path: Path,
                 preset: str, results: dict, remaining, dry_run: bool):
    lines = []
    add = lines.append
    bar = "=" * 70
    add(bar)
    add("PDF REDACTION " + ("DRY-RUN (preview) " if dry_run else "") + "REPORT")
    add(bar)
    add(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)")
    add(f"Input     : {input_path}")
    add(f"Output    : {'(none — dry run, no file written)' if dry_run else output_path}")
    add(f"Preset    : {preset}")
    add(f"Pages     : {results['page_count']}")
    add("")

    add("-" * 70)
    add("MATCHES BY CATEGORY")
    add("-" * 70)
    if results["counts"]:
        for cat in sorted(results["counts"]):
            label = CATEGORY_LABELS.get(cat, cat)
            add(f"  {label:<32} {results['counts'][cat]:>4} match(es)")
    else:
        add("  No matches found for the enabled categories.")
    add("")

    add("-" * 70)
    add("DETAIL (page / category / matched text)")
    add("-" * 70)
    if results["matches"]:
        for page_no, cat, matched, located in results["matches"]:
            label = CATEGORY_LABELS.get(cat, cat)
            # Dry-run shows full values so you can verify accuracy before
            # committing; the final report masks them so the report itself
            # is safe to keep next to the redacted PDF.
            shown = " ".join(matched.split()) if dry_run else mask_value(matched)
            flag = "" if located else "   << NOT LOCATED — see warnings"
            add(f"  p.{page_no:<3} {label:<28} {shown}{flag}")
    else:
        add("  (none)")
    add("")

    if results["embedded_files"] or results["toc_redacted"]:
        add("-" * 70)
        add("DOCUMENT-LEVEL ITEMS")
        add("-" * 70)
    if results["embedded_files"]:
        n = len(results["embedded_files"])
        add(f"  EMBEDDED FILE ATTACHMENTS: {n} attachment(s) "
            + ("WOULD BE removed" if dry_run else "removed")
            + " (contents are not scanned — they may carry anything):")
        for name in results["embedded_files"]:
            add(f"    - {name}")
        add("")
    if results["toc_redacted"]:
        add(f"  BOOKMARKS: {results['toc_redacted']} bookmark title(s) "
            + ("WOULD BE" if dry_run else "were")
            + " replaced with '[redacted]'.")
        add("")

    if results["ocr_pages"]:
        add("-" * 70)
        add("OCR")
        add("-" * 70)
        add(f"  Pages {results['ocr_pages']} are scanned images with no text")
        add("  layer. They were OCR'd to locate sensitive text, and the")
        add("  matching image regions were permanently blanked out.")
        add("  Note: OCR accuracy is not perfect — review these pages.")
        add("")

    warnings = []
    if results["scanned_pages"]:
        if results.get("ocr_enabled"):
            warnings.append(
                f"UNREADABLE SCANNED PAGES: {results['scanned_pages']} are "
                f"image-only and OCR could not extract text from them. "
                f"NOTHING WAS REDACTED on these pages — review them manually "
                f"or pre-process with 'ocrmypdf input.pdf searchable.pdf'.")
        else:
            warnings.append(
                f"SCANNED/IMAGE-ONLY PAGES: {results['scanned_pages']} have "
                f"no searchable text and OCR is off (not installed, or "
                f"disabled via 'options: ocr: false' in the config), so "
                f"NOTHING WAS REDACTED on these pages. To enable OCR: "
                f"'brew install tesseract' and set the option to true.")
    if results["unlocated"]:
        warnings.append(
            "MATCHES NOT LOCATED VISUALLY: the items flagged '<< NOT LOCATED' "
            "were found in the text layer but their on-page position could "
            "not be determined (usually text split across lines). They were "
            "NOT redacted — review those pages manually.")
    if results["image_pages"]:
        if HAVE_ZBAR:
            redacted = set(results["barcode_pages"])
            flagged = [p for p in results["image_pages"] if p not in redacted]
            if results["barcode_pages"]:
                warnings.append(
                    f"QR/BARCODES: decoded and redacted on pages "
                    f"{sorted(redacted)}.")
            if flagged:
                warnings.append(
                    f"IMAGES PRESENT on pages {flagged}: no barcode decoded, "
                    f"but images may still contain QR codes, signatures, "
                    f"logos, or scanned identifiers. Review manually.")
        else:
            warnings.append(
                f"IMAGES PRESENT on pages {results['image_pages']}: these may "
                f"contain QR codes, barcodes, or signatures. Automatic "
                f"barcode detection is OFF (install optional extras: "
                f"'brew install zbar && pip install pyzbar pillow'). "
                f"Review these pages manually.")

    add("-" * 70)
    add("WARNINGS")
    add("-" * 70)
    if warnings:
        for w in warnings:
            for i, line in enumerate(_wrap(w, 66)):
                add(("  ! " if i == 0 else "    ") + line)
            add("")
    else:
        add("  None.")
        add("")

    add("-" * 70)
    add("POST-REDACTION VERIFICATION")
    add("-" * 70)
    if dry_run:
        add("  Skipped (dry run — no output file was created).")
    elif remaining is None:
        add("  Skipped.")
    elif not remaining:
        add("  PASS — the redacted PDF was re-opened and re-scanned with the")
        add("  same patterns: 0 remaining matches. The text was permanently")
        add("  removed from the file.")
        if results["ocr_pages"]:
            add(f"  OCR'd pages {results['ocr_pages']} were re-OCR'd to "
                f"confirm the scanned")
            add("  images themselves no longer show the sensitive text.")
    else:
        add("  *** FAIL — re-scan of the OUTPUT file still finds matches: ***")
        for cat, n in sorted(remaining.items()):
            add(f"    {CATEGORY_LABELS.get(cat, cat)}: {n}")
        add("  Do NOT share this file. Review it manually.")
    add("")
    add("Reminder: automated redaction is a first pass, not a guarantee.")
    add("Always skim the output PDF before sharing it.")
    add(bar)

    report_path.write_text("\n".join(lines), encoding="utf-8")


def _wrap(text: str, width: int) -> list:
    """Simple word wrap for report warnings."""
    words, out, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Locally redact sensitive identifiers from a searchable "
                    "PDF. Produces a redacted PDF plus a verification report. "
                    "Nothing leaves your Mac.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 redact.py statement.pdf --preset financial\n"
               "  python3 redact.py labs.pdf --preset medical --dry-run\n"
               "  python3 redact.py doc.pdf -o clean.pdf --config redact_config.yaml\n")
    parser.add_argument("input", nargs="?", help="Path to the PDF to redact")
    parser.add_argument("-p", "--preset", choices=sorted(PRESETS),
                        help="Document-type preset (default: the config's "
                             "'preset' setting, else general = all categories)")
    parser.add_argument("-o", "--output",
                        help="Output PDF path (default: <input>_redacted.pdf)")
    parser.add_argument("-c", "--config",
                        help="Alternate YAML config with custom terms/"
                             "patterns (default: config/redact_config.yaml, "
                             "used on every run)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Preview only: report what WOULD be redacted; "
                             "no PDF is written")
    parser.add_argument("--categories",
                        help="Comma-separated category list overriding the "
                             "preset (see --list-categories)")
    parser.add_argument("--report", help="Report path (default: <output>_report.txt)")
    parser.add_argument("--list-categories", action="store_true",
                        help="List available categories and presets, then exit")
    args = parser.parse_args()

    if args.list_categories:
        print("Categories:")
        for cat in CATEGORY_PATTERNS:
            print(f"  {cat:<16} {CATEGORY_LABELS[cat]}")
        print("\nPresets:")
        for name, cats in PRESETS.items():
            print(f"  {name:<10} {', '.join(cats)}")
        return

    if not args.input:
        parser.error("input PDF is required (or use --list-categories)")
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        sys.exit(f"Input must be a PDF: {input_path}")

    output_path = (Path(args.output).expanduser() if args.output
                   else input_path.with_name(input_path.stem + "_redacted.pdf"))
    report_path = (Path(args.report).expanduser() if args.report
                   else output_path.with_name(output_path.stem + "_report.txt"))

    # The default config is applied on EVERY run; --config swaps in another
    # one. An explicitly named config that doesn't exist is an error (never
    # silently redact less than the user asked for).
    if args.config:
        config_path = Path(args.config).expanduser()
        if not config_path.exists() and not config_path.is_absolute():
            alt = PROJECT_ROOT / args.config
            config_path = alt if alt.exists() else config_path
        if not config_path.exists():
            sys.exit(f"Config file not found: {config_path}")
    else:
        config_path = Path(DEFAULT_CONFIG)
        if not config_path.exists():
            config_path = PROJECT_ROOT / DEFAULT_CONFIG
    cfg = load_config(config_path)

    # Preset: command line beats the config's 'preset', which beats general.
    preset = args.preset or cfg.get("preset") or "general"
    if preset not in PRESETS:
        sys.exit(f"Unknown preset in config: {preset!r} "
                 f"(choose from: {', '.join(sorted(PRESETS))})")

    # Assemble enabled categories:
    #   --categories = exact explicit list (config switches don't apply);
    #   otherwise preset, adjusted by the config's categories: switches
    #   (true = always redact, false = never, unset = follow the preset).
    forced_on, forced_off = [], []
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",") if c.strip()}
        unknown = [c for c in wanted if c not in CATEGORY_PATTERNS]
        if unknown:
            sys.exit(f"Unknown categories: {', '.join(unknown)} "
                     f"(use --list-categories)")
    else:
        wanted = set(PRESETS[preset])
        switches = cfg.get("categories") or {}
        unknown = [c for c in switches if c not in CATEGORY_PATTERNS]
        if unknown:
            sys.exit(f"Unknown categories in config: {', '.join(unknown)} "
                     f"(use --list-categories)")
        forced_on = sorted(c for c, v in switches.items() if v is True)
        forced_off = sorted(c for c, v in switches.items() if v is False)
        wanted |= set(forced_on)
        wanted -= set(forced_off)
    categories = {c: CATEGORY_PATTERNS[c] for c in CATEGORY_PATTERNS
                  if c in wanted}

    custom = build_custom_patterns(cfg)
    if custom:
        categories["custom"] = custom

    # Behavior toggles from the config's options: section.
    opts = cfg.get("options") or {}
    use_ocr = bool(opts.get("ocr", True))
    redact_barcodes = bool(opts.get("redact_barcodes", True))

    mode = "DRY RUN (preview only)" if args.dry_run else "REDACT"
    print(f"Mode    : {mode}")
    print(f"Input   : {input_path}")
    print(f"Config  : {config_path}" if config_path.exists()
          else f"Config  : (none — {config_path} not found; no custom terms)")
    print(f"Preset  : {preset}  "
          f"({len(categories)} categories"
          f"{', incl. ' + str(len(custom)) + ' custom term(s)/pattern(s)' if custom else ''})")
    if forced_on or forced_off:
        parts = []
        if forced_on:
            parts.append("always: " + ", ".join(forced_on))
        if forced_off:
            parts.append("never: " + ", ".join(forced_off))
        print(f"Switches: {';  '.join(parts)}  (from config)")
    if preset in PRESET_NOTES:
        print(f"Note    : {PRESET_NOTES[preset]}")

    exclude = tuple(str(t).lower() for t in (cfg.get("exclude_terms") or [])
                    if len(str(t).strip()) >= 2)

    results = process_pdf(input_path, output_path, categories,
                          dry_run=args.dry_run,
                          redact_barcodes=redact_barcodes,
                          exclude=exclude, use_ocr=use_ocr)

    # Hard stop if no page yielded any text (even via OCR): be honest.
    if results["total_text_chars"] == 0:
        print("\n*** This PDF has NO searchable text — it appears to be a "
              "scanned/image-based document. ***")
        if results["ocr_enabled"]:
            print("*** OCR ran but could not extract any text. NOTHING was "
                  "redacted.\n"
                  "*** Try pre-processing with ocrmypdf, or review/redact "
                  "manually.")
        elif not TESSDATA:
            print("*** OCR is not installed, so NOTHING was redacted.\n"
                  "*** Install it and re-run:\n"
                  "***   brew install tesseract")
        else:
            print("*** OCR is disabled in your config (options: ocr: false), "
                  "so NOTHING was redacted.")
        if not args.dry_run and output_path.exists():
            output_path.unlink()  # don't leave a misleading 'redacted' copy
        sys.exit(2)

    remaining = None
    if not args.dry_run:
        remaining = verify_output(output_path, categories,
                                  ocr_pages=set(results["ocr_pages"]),
                                  exclude=exclude)

    write_report(report_path, input_path, output_path, preset,
                 results, remaining, args.dry_run)

    total = sum(results["counts"].values())
    print(f"\nMatches : {total} across {len(results['counts'])} categories")
    if results["ocr_pages"]:
        print(f"OCR     : scanned pages {results['ocr_pages']} were OCR'd; "
              f"matching image regions blanked.")
    if results["scanned_pages"]:
        print(f"WARNING : pages {results['scanned_pages']} are image-only "
              f"and could not be read. NOT redacted"
              + ("." if results["ocr_enabled"]
                 else " — OCR is off (install tesseract / enable in config)."))
    if results["unlocated"]:
        print(f"WARNING : {len(results['unlocated'])} match(es) could not be "
              f"located on the page and were NOT redacted — see report.")
    if not args.dry_run:
        print(f"Output  : {output_path}")
        if remaining == {}:
            print("Verify  : PASS — output re-scanned, 0 remaining matches.")
        else:
            print("Verify  : *** FAIL — sensitive text remains in output. "
                  "See report. Do not share the file. ***")
    print(f"Report  : {report_path}")
    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to write the "
              "redacted PDF.")

    # Exit codes let scripts/run.sh classify the outcome honestly:
    #   3 = verification failed (sensitive text remains in the output)
    #   2 = some pages were unreadable and are NOT redacted
    if remaining:
        sys.exit(3)
    if results["scanned_pages"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
