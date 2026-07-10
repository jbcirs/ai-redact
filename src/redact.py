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
import os
import re
import sys
import tempfile
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

# zxing-cpp decodes QR codes/barcodes inside page images so they can be
# redacted. It's a self-contained pip wheel (installed by scripts/run.sh via
# requirements.txt) — no system library. Without it, pages containing images
# are only flagged for manual review.
try:
    import io

    import zxingcpp
    from PIL import Image

    HAVE_BARCODE = True
except Exception:
    HAVE_BARCODE = False


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
        # Boundary guards reject digit continuations ("...9999.5") but must
        # allow sentence punctuation ("...9999." at end of a sentence).
        _p(r"(?<!\d)(?<!\d[.\-])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)(?![.\-]\d)", flags=0),
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
        # or decimal). Long enough that balances/prices never match. Trailing
        # guard rejects digit continuations but allows sentence punctuation.
        _p(r"(?<![\d.,$\-])\d{8,17}(?!\d)(?![.,]\d)", flags=0),
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
    "face": "Detected faces (Vision)",
    "handwriting": "Handwriting regions (Vision)",
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


def flatten_terms(value):
    """Yield term strings from a config value.

    Users write custom_terms either as a flat list or grouped under
    headings (names:, addresses:, …) — both must work:

        custom_terms:            custom_terms:
          - "John Smith"           names:
          - "123 Maple Ave"          - "John Smith"
                                   addresses:
                                     - "123 Maple Ave"
    """
    if isinstance(value, dict):
        for group in value.values():
            for term in flatten_terms(group):
                yield term
    elif isinstance(value, (list, tuple)):
        for item in value:
            for term in flatten_terms(item):
                yield term
    elif value is not None:
        term = str(value).strip()
        if term:
            yield term


def build_custom_patterns(cfg: dict):
    """Turn config custom_terms / custom_patterns into pattern dicts."""
    patterns = []
    for term in flatten_terms(cfg.get("custom_terms")):
        if len(term) < 2:
            # 1-char terms would redact far too much; never skip silently.
            print(f"  ! Ignoring custom term {term!r}: too short to match safely")
            continue
        # Whole-word, case-insensitive match. Whitespace inside the term
        # matches ANY whitespace run, so "Jane Smith" still matches when a
        # PDF renders it as "Jane  Smith" or splits it across a line break.
        esc = r"\s+".join(re.escape(part) for part in term.split())
        patterns.append(_p(r"(?<!\w)" + esc + r"(?!\w)"))
    if cfg.get("custom_terms") and not patterns:
        sys.exit("Config error: custom_terms contains no usable terms — "
                 "check its format (see docs/CONFIGURATION.md).")
    for item in cfg.get("custom_patterns") or []:
        if isinstance(item, dict) and item.get("regex"):
            try:
                patterns.append(_p(item["regex"]))
            except re.error as e:
                print(f"  ! Skipping invalid custom pattern "
                      f"'{item.get('name', item['regex'])}': {e}")
    return patterns


def resolve_categories(cfg: dict, preset_arg: str = None, categories_arg: str = None):
    """Build (preset, categories, exclude, opts) from a loaded config plus
    the same CLI overrides main() accepts. Shared with combine_outputs.py
    (docs/plans/expansion-plan.md §3.H) so the combined PDF's re-verify
    pass uses the exact same detector set as the per-file run it merges."""
    preset = preset_arg or cfg.get("preset") or "general"
    if preset not in PRESETS:
        sys.exit(f"Unknown preset in config: {preset!r} "
                 f"(choose from: {', '.join(sorted(PRESETS))})")

    forced_on, forced_off = [], []
    if categories_arg:
        wanted = {c.strip() for c in categories_arg.split(",") if c.strip()}
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

    opts = cfg.get("options") or {}
    exclude = tuple(t.lower() for t in flatten_terms(cfg.get("exclude_terms"))
                    if len(t) >= 2)
    return preset, categories, exclude, opts, forced_on, forced_off, custom


def resolve_password(input_path: Path, password_arg: str, cfg: dict) -> str:
    """--password (one-off) beats the config's passwords: filename map
    (batch runs). Never logged, never echoed (docs/plans/expansion-plan.md
    §2a.3, §6 grill item 6) — only ever passed to an authenticate() call."""
    if password_arg:
        return password_arg
    passwords = cfg.get("passwords")
    if isinstance(passwords, dict):
        return passwords.get(input_path.name)
    return None


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
    # Scan the raw text AND a whitespace-normalized copy: a value wrapped
    # across a line break ("(555)\n010-9999") defeats single-whitespace
    # separators in the patterns, but matches in the normalized copy.
    # locate_on_page and search_for handle line-crossing needles.
    norm = " ".join(text.split())
    sources = (text, norm) if norm != text else (text,)
    found = {}
    for category, patterns in categories.items():
        for pat in patterns:
            for src in sources:
                for m in pat["regex"].finditer(src):
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

    If zxing-cpp is available, decode each embedded image and return the
    page rectangles of any barcode/QR found so they can be redacted.
    Otherwise just report whether the page contains images at all (flagged
    for manual review in the report).
    """
    images = page.get_images(full=True)
    if not images:
        return [], False
    if not HAVE_BARCODE:
        return [], True
    barcode_rects = []
    for img in images:
        xref = img[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4:  # CMYK etc. -> RGB
                pix = fitz.Pixmap(fitz.csRGB, pix)
            pil = Image.open(io.BytesIO(pix.tobytes("png")))
            if zxingcpp.read_barcodes(pil):
                barcode_rects.extend(page.get_image_rects(xref))
        except Exception:
            continue  # unreadable image; it stays flagged via has_images
    return barcode_rects, True


# ---------------------------------------------------------------------------
# Main redaction pass
# ---------------------------------------------------------------------------
def process_pdf(input_path: Path, output_path: Path, categories: dict,
                dry_run: bool, redact_barcodes: bool, exclude=(),
                use_ocr: bool = True, password: str = None,
                vision_opts: dict = None):
    """Scan (and unless dry_run, redact) the PDF. Returns a results dict.

    Raises EncryptedFileError if the PDF needs a password and the one
    given (or none) doesn't open it — never a silent skip (§3.G)."""
    doc = fitz.open(input_path)
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            raise EncryptedFileError(
                f"{input_path.name} is password-protected and could not "
                f"be opened ({'wrong password' if password else 'no password given'})."
                " Use --password, or add it to the config's passwords: map.")
    ocr_enabled = bool(TESSDATA) and use_ocr
    vision_opts = vision_opts or {}
    do_faces = bool(vision_opts.get("redact_faces"))
    do_handwriting = bool(vision_opts.get("redact_handwriting"))
    do_handwriting_ocr = bool(vision_opts.get("handwriting_ocr"))
    vision_active = do_faces or do_handwriting or do_handwriting_ocr

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
        "faces_redacted": 0,           # count, Vision redact_faces
        "handwriting_redacted": 0,     # count, Vision redact_handwriting
        "vision_pages": [],            # pages Vision actually ran on
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

        # --- Apple Vision: handwriting/face redaction (opt-in, default
        # off) — runs on any page with image content, since a signature
        # or photo can sit on an otherwise normal text page, not just on
        # scanned pages (expansion-plan.md §3.D). ---
        vision_ran_this_page = False
        if vision_active and has_images:
            from handlers import vision_helper
            try:
                png_bytes, img_w, img_h = vision_helper.page_to_png(page)
                results["vision_pages"].append(page_no)
                vision_ran_this_page = True
                if do_faces:
                    faces = vision_helper.detect_faces(png_bytes)
                    for nbbox in faces:
                        rect = vision_helper.map_bbox_to_page_rect(
                            nbbox, img_w, img_h, page.rect)
                        results["faces_redacted"] += 1
                        results["total_text_chars"] += 1  # "readable" signal
                        if not dry_run:
                            page.add_redact_annot(rect, fill=(0, 0, 0))
                if do_handwriting or do_handwriting_ocr:
                    observations = vision_helper.recognize_text(png_bytes)
                    for obs_text, nbbox in observations:
                        results["total_text_chars"] += len(obs_text)
                        rect = vision_helper.map_bbox_to_page_rect(
                            nbbox, img_w, img_h, page.rect)
                        if do_handwriting:
                            # Blanket: every observation, matched or not —
                            # the only mechanism that reliably catches a
                            # signature (§5.1: signature-specific detection
                            # does not exist as a local capability).
                            results["handwriting_redacted"] += 1
                            if not dry_run:
                                page.add_redact_annot(rect, fill=(0, 0, 0))
                        else:
                            hits = find_matches_in_text(
                                obs_text, categories, exclude)
                            if hits:
                                for category, matched in hits:
                                    results["counts"][category] = (
                                        results["counts"].get(category, 0) + 1)
                                    results["matches"].append(
                                        (page_no, category, matched, True))
                                if not dry_run:
                                    page.add_redact_annot(rect, fill=(0, 0, 0))
            except UnsupportedFormatError as e:
                # Vision unavailable (e.g. dependency missing) — reported
                # once via a note, never a silent skip; text/barcode
                # redaction on this page still proceeds normally.
                results.setdefault("notes", []).append(str(e))
                vision_active = False

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
            elif not vision_ran_this_page:
                results["scanned_pages"].append(page_no)
                continue  # can't read this page — reported, never faked
            else:
                # Tesseract found nothing, but Vision already reviewed this
                # page above (found matches/faces/handwriting or genuinely
                # found none) — it's reviewed, not "unreadable"; there's no
                # text-layer content for the standard match pass below.
                continue

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
                  exclude=(), vision_pages=(), vision_opts=None) -> dict:
    """Re-open the redacted PDF and re-run every pattern.

    Proves the sensitive text is actually GONE from the file, not hidden.
    Pages that were redacted via OCR are OCR'd AGAIN here, so we verify the
    scanned image itself no longer shows the sensitive text. Link targets
    and bookmark titles are re-checked too. Pages Vision touched are
    re-scanned with Vision too (faces/handwriting must be gone, not just
    the plain-text matches — expansion-plan.md §3.D).
    Returns {category: remaining_count}.
    """
    doc = fitz.open(output_path)
    remaining = {}

    def count(found):
        for category, _ in found:
            remaining[category] = remaining.get(category, 0) + 1

    vision_opts = vision_opts or {}
    do_faces = bool(vision_opts.get("redact_faces"))
    do_handwriting = bool(vision_opts.get("redact_handwriting"))
    do_handwriting_ocr = bool(vision_opts.get("handwriting_ocr"))

    for page in doc:
        text = page.get_text("text")
        if not text.strip() and (page.number + 1) in ocr_pages:
            _, text = ocr_page(page)
        count(find_matches_in_text(text, categories, exclude))
        for link in page.get_links():
            uri = link.get("uri") or ""
            if uri and find_matches_in_text(uri, categories, exclude):
                remaining["link"] = remaining.get("link", 0) + 1
        if (page.number + 1) in vision_pages and (
            do_faces or do_handwriting or do_handwriting_ocr):
            from handlers import vision_helper
            try:
                png_bytes, _, _ = vision_helper.page_to_png(page)
                if do_faces:
                    n_faces = len(vision_helper.detect_faces(png_bytes))
                    if n_faces:
                        remaining["face"] = remaining.get("face", 0) + n_faces
                if do_handwriting or do_handwriting_ocr:
                    observations = vision_helper.recognize_text(png_bytes)
                    if do_handwriting and observations:
                        remaining["handwriting"] = (
                            remaining.get("handwriting", 0) + len(observations))
                    elif do_handwriting_ocr:
                        for obs_text, _ in observations:
                            count(find_matches_in_text(
                                obs_text, categories, exclude))
            except UnsupportedFormatError:
                pass  # already noted during redaction; don't double-report
    for entry in doc.get_toc(simple=True):
        count(find_matches_in_text(str(entry[1]), categories, exclude))
    doc.close()
    return remaining


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(report_path: Path, input_path: Path, output_path: Path,
                 preset: str, results: dict, remaining, dry_run: bool,
                 conversion: dict = None):
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

    if conversion:
        add("-" * 70)
        add("CONVERSION")
        add("-" * 70)
        add(f"  Converted to PDF via {conversion.get('converter', '?')} "
            f"(layout simplified; content-faithful).")
        dropped = conversion.get("dropped_elements", 0)
        if dropped:
            add(f"  ! {dropped} element(s) could NOT be converted and are "
                f"OMITTED from the")
            add("    output (omitted content cannot leak, but review the "
                "original if needed).")
        for note in conversion.get("notes", []):
            for i, line in enumerate(_wrap(str(note), 66)):
                add(("  - " if i == 0 else "    ") + line)
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

    if results.get("vision_pages"):
        add("-" * 70)
        add("APPLE VISION (handwriting / faces)")
        add("-" * 70)
        add(f"  Pages {results['vision_pages']} were scanned with Apple's "
            f"on-device Vision")
        add(f"  framework: {results.get('faces_redacted', 0)} face(s) redacted, "
            f"{results.get('handwriting_redacted', 0)} handwriting")
        add("  region(s) redacted (blanket mode).")
        add("  Note: detection is best-effort, not a guarantee — see README/")
        add("  docs/CONFIGURATION.md. Always skim these pages before sharing.")
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
        if HAVE_BARCODE:
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
                f"barcode detection is OFF (reinstall dependencies: "
                f"'.venv/bin/pip install -r requirements.txt'). "
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
# Format routing — dispatch non-PDF inputs to src/handlers/* modules
# ---------------------------------------------------------------------------
from handlers.common import EncryptedFileError, UnsupportedFormatError  # noqa: E402

TEXT_EXTENSIONS = {".txt", ".md", ".log", ".json", ".yaml", ".yml",
                   ".xml", ".html", ".htm"}
CSV_EXTENSIONS = {".csv", ".tsv"}
OFFICE_EXTENSIONS = {".docx", ".pptx"}
EXCEL_EXTENSIONS = {".xlsx"}
# Legacy Word family (docs/plans/expansion-plan.md §3.A) — converted via
# macOS textutil into .docx, then the Tier-1 docx pipeline. NOTE: RTF is
# plain ASCII text, so it MUST be intercepted here, before the generic
# _sniffs_as_text() fallback below — otherwise it would be silently
# misrouted to text_handler, which would read/write raw RTF control codes
# as if they were plain content and corrupt the file (found during the
# review pass in docs/plans/expansion-plan.md §2a.1).
LEGACY_OFFICE_EXTENSIONS = {".doc", ".odt", ".rtf"}
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def route_input(path: Path):
    """Classify the input by magic bytes first, extension second —
    extensions lie, and a PDF renamed .png must still hit the PDF path.
    Returns (kind, handler_module_or_None)."""
    with open(path, "rb") as f:
        head = f.read(8)
    ext = path.suffix.lower()
    if head.startswith(b"%PDF"):
        return "pdf", None  # includes PDF-compatible .ai files
    # RTF and EML are both plain ASCII and would otherwise fall into the
    # text-sniff fallback; intercept on magic bytes OR extension so a
    # mislabeled file fails loud via its real handler instead of being
    # silently treated as plain text.
    if head.startswith(b"{\\rtf") or ext == ".rtf":
        from handlers import legacy_office_handler
        return "legacy_office", legacy_office_handler
    if ext == ".eml":
        from handlers import email_handler
        return "email", email_handler
    if head.startswith(_OLE2_MAGIC) or ext in (".doc", ".msg"):
        # OLE2 compound file: legacy .doc, .xls, .ppt, .msg all share this
        # exact magic — the byte signature alone cannot distinguish them,
        # so the extension picks the family.
        if ext == ".doc":
            from handlers import legacy_office_handler
            return "legacy_office", legacy_office_handler
        if ext == ".msg":
            from handlers import email_handler
            return "email", email_handler
        if ext in (".xls", ".ppt"):
            # No Tier-1 pure-Python reader exists for these binary formats
            # — LibreOffice (or, if ever restored, MS Office automation)
            # is the only path. Handler is resolved at dispatch time from
            # the office_converter config setting, not here.
            return "office_binary", None
        if ext in (".docx", ".xlsx", ".pptx"):
            # Password-protected Office files are OLE2-wrapped (MS-OFFCRYPTO)
            # even though the unencrypted format is a zip — the byte
            # signature alone distinguishes "encrypted" from "not", the
            # extension says which decrypted format to expect afterward.
            return "encrypted_office", None
        return "unsupported", None
    if head.startswith(b"PK\x03\x04"):
        if ext in OFFICE_EXTENSIONS:
            # Handler is resolved at dispatch time (office_converter may
            # request LibreOffice fidelity instead of the Tier-1 default).
            return "office", None
        if ext in EXCEL_EXTENSIONS:
            from handlers import excel_handler
            return "excel", excel_handler
        if ext == ".odt" and _is_odf_text(path):
            from handlers import legacy_office_handler
            return "legacy_office", legacy_office_handler
        if ext == ".odp" and _is_odf_presentation(path):
            return "office_binary", None
        if ext == ".epub" and _is_epub(path):
            from handlers import epub_handler
            return "epub", epub_handler
        return "unsupported", None
    from handlers import image_handler
    if ext in image_handler.SUPPORTED_EXTENSIONS:
        return "image", image_handler
    if ext in CSV_EXTENSIONS:
        from handlers import csv_handler
        return "csv", csv_handler
    if ext in TEXT_EXTENSIONS or (
        ext not in LEGACY_OFFICE_EXTENSIONS and _sniffs_as_text(path)
    ):
        from handlers import text_handler
        return "text", text_handler
    return "unsupported", None


def _zip_mimetype(path: Path) -> str:
    """The zip's uncompressed 'mimetype' entry, or '' if unreadable. Never
    raises — an unreadable/odd zip just isn't the ODF type being checked."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read("mimetype").decode("ascii", "replace").strip()
    except Exception:
        return ""


def _is_odf_text(path: Path) -> bool:
    return _zip_mimetype(path) == "application/vnd.oasis.opendocument.text"


def _is_odf_presentation(path: Path) -> bool:
    return _zip_mimetype(path) == "application/vnd.oasis.opendocument.presentation"


def _is_epub(path: Path) -> bool:
    return _zip_mimetype(path) == "application/epub+zip"


def _sniffs_as_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.read(4096).decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ---------------------------------------------------------------------------
# office_converter resolution — picks the converter for docx/pptx/xls/ppt/odp
# (docs/plans/expansion-plan.md §3.B). Tier-1 (pure Python, "simple") only
# exists for .docx/.pptx; .xls/.ppt/.odp have no Tier-1 reader at all.
# ---------------------------------------------------------------------------
_TIER1_CAPABLE_EXTENSIONS = {".docx", ".pptx"}


def detect_msoffice_app(ext: str) -> bool:
    """MS Office automation is DESCOPED (expansion-plan.md §3.C) — no
    working AppleScript PDF export exists on the tested Word build. Always
    False; kept as a named check so the resolver's shape doesn't need to
    change if a future Office release restores a verified export path."""
    return False


def resolve_office_handler(ext: str, opts: dict):
    """Pick the converter module for an office-family file per the config's
    options.office_converter (auto|simple|msoffice|libreoffice). Raises
    UnsupportedFormatError with an actionable message rather than ever
    silently falling back to a different mode than what was explicitly
    requested."""
    mode = (opts or {}).get("office_converter") or "auto"
    tier1_ok = ext in _TIER1_CAPABLE_EXTENSIONS

    def _simple():
        if not tier1_ok:
            raise UnsupportedFormatError(
                f"'{ext}' has no Tier-1 (pure-Python) reader — set "
                f"office_converter: libreoffice in the config, or export "
                f"the file as .docx/.pptx.")
        from handlers import office_handler
        return office_handler

    def _libreoffice():
        from handlers import libreoffice_handler
        if not libreoffice_handler.find_soffice():
            raise UnsupportedFormatError(
                "LibreOffice is not installed. Install it with "
                "'brew install --cask libreoffice' (free, ~700 MB, fully "
                "offline), or set office_converter accordingly.")
        return libreoffice_handler

    def _msoffice():
        if not detect_msoffice_app(ext):
            raise UnsupportedFormatError(
                "office_converter: msoffice is not implemented — this "
                "Word/Excel/PowerPoint build has no working AppleScript "
                "PDF export (verified by live-testing; see "
                "docs/plans/expansion-plan.md §3.C). Use "
                "office_converter: libreoffice or simple instead.")
        from handlers import msoffice_handler  # pragma: no cover
        return msoffice_handler

    if mode == "simple":
        return _simple()
    if mode == "libreoffice":
        return _libreoffice()
    if mode == "msoffice":
        return _msoffice()
    if mode != "auto":
        raise UnsupportedFormatError(
            f"unknown office_converter setting {mode!r} — choose from: "
            f"auto, simple, msoffice, libreoffice.")
    # auto: LibreOffice if installed, else Tier-1 simple for docx/pptx,
    # else refuse (xls/ppt/odp with no LibreOffice have no fallback).
    from handlers import libreoffice_handler
    if libreoffice_handler.find_soffice():
        return libreoffice_handler
    if tier1_ok:
        from handlers import office_handler
        return office_handler
    raise UnsupportedFormatError(
        f"'{ext}' needs LibreOffice to convert (not installed) — install "
        f"it with 'brew install --cask libreoffice' (free, ~700 MB, fully "
        f"offline), or export the file as .docx/.pptx/.xlsx and re-run.")


def decrypt_office_bytes(input_path: Path, password: str) -> bytes:
    """Decrypt a password-protected .docx/.xlsx/.pptx to plain OOXML zip
    bytes via msoffcrypto-tool (docs/plans/expansion-plan.md §3.G, spiked
    live: encrypt/decrypt/wrong-password round-tripped correctly on this
    machine). Raises EncryptedFileError — never a silent skip — on a
    missing/wrong password or any decrypt failure."""
    import io

    try:
        import msoffcrypto
        from msoffcrypto.exceptions import DecryptionError, InvalidKeyError
    except ImportError as exc:
        raise EncryptedFileError(
            "msoffcrypto-tool is not installed — run "
            "'pip install -r requirements.txt' to open encrypted Office "
            "files."
        ) from exc

    if not password:
        raise EncryptedFileError(
            f"{input_path.name} is password-protected — use --password, "
            f"or add it to the config's passwords: map.")

    with open(input_path, "rb") as f:
        office_file = msoffcrypto.OfficeFile(f)
        try:
            office_file.load_key(password=password)
            out = io.BytesIO()
            office_file.decrypt(out)
        except (InvalidKeyError, DecryptionError) as exc:
            raise EncryptedFileError(
                f"{input_path.name}: wrong password."
            ) from exc
        except Exception as exc:
            raise EncryptedFileError(
                f"{input_path.name}: could not decrypt ({exc})."
            ) from exc
    return out.getvalue()


def run_decrypted_office_flow(input_path, password, args, preset, categories,
                              exclude, opts, use_ocr, redact_barcodes,
                              dry_run):
    """kind == 'encrypted_office': decrypt to a temp file (named exactly
    like the original so resolve_outputs() derives the same output name
    it would for an unencrypted file of that name), then dispatch through
    the normal pipeline. Returns the exit code."""
    try:
        decrypted = decrypt_office_bytes(input_path, password)
    except EncryptedFileError as e:
        print(f"Encrypted: {e}")
        return 5

    # Default (-o not given) must land next to the ORIGINAL file, not the
    # ephemeral temp dir the decrypted copy briefly lives in.
    dispatch_args = args
    if not args.output:
        dispatch_args = argparse.Namespace(**vars(args))
        dispatch_args.output = str(input_path.parent)

    with tempfile.TemporaryDirectory(prefix="ai-redact-decrypt-") as tmp:
        tmp_path = Path(tmp) / input_path.name
        tmp_path.write_bytes(decrypted)
        kind, handler = route_input(tmp_path)
        if kind == "unsupported":
            print(f"Unsupported: decrypted {input_path.name} did not "
                  f"look like a valid Office file")
            return 4
        try:
            if kind in ("office", "office_binary"):
                handler = resolve_office_handler(tmp_path.suffix.lower(), opts)
            if kind in ("image", "office", "office_binary", "legacy_office",
                       "epub"):
                return run_convert_flow(handler, kind, dispatch_args, tmp_path,
                                        preset, categories, exclude, opts,
                                        use_ocr, redact_barcodes, dry_run)
            if kind in ("text", "csv", "excel"):
                return run_native_flow(handler, kind, dispatch_args, tmp_path,
                                       preset, categories, exclude, opts,
                                       dry_run)
        except UnsupportedFormatError as e:
            print(f"Unsupported: {e}")
            return 4
    return 4


def resolve_outputs(args, input_path: Path, out_ext: str):
    """Name outputs <stem>_redacted.<ext>, inserting the original extension
    when the format changes (foo.docx -> foo_docx_redacted.pdf) so two
    inputs with the same stem can't collide in output/."""
    if input_path.suffix.lower() == out_ext:
        name = f"{input_path.stem}_redacted{out_ext}"
    else:
        orig = input_path.suffix.lower().lstrip(".")
        name = f"{input_path.stem}_{orig}_redacted{out_ext}"
    if args.output:
        op = Path(args.output).expanduser()
        output_path = op / name if op.is_dir() else op
    else:
        output_path = input_path.with_name(name)
    # NOTE: report_path must be derived from output_path.name, not .stem.
    # .stem only strips the FINAL extension, so foo_redacted.txt and
    # foo_redacted.yaml (same stem, different native-format extensions —
    # a completely normal same-directory batch) both collapsed to the
    # identical "foo_redacted_report.txt", silently overwriting each
    # other's audit trail. Found while building the combine feature
    # (docs/plans/expansion-plan.md §3.H), which trusts each file's report
    # to gate whether it's safe to merge — a collision there would have
    # let a FAILed file's stale PASS report wave it into a shared PDF.
    report_path = (Path(args.report).expanduser() if args.report
                   else output_path.with_name(output_path.name + "_report.txt"))
    return output_path, report_path


def tabular_categories(categories: dict) -> dict:
    """Detector set for spreadsheets/CSV: drop the bare digit-run account
    pattern. In tables it would mass-redact amounts-in-cents, share counts,
    and order IDs — violating the never-redact-financial-data rule. Labeled
    account numbers still match via header-as-context scanning in the
    tabular handlers."""
    out = dict(categories)
    if "account_number" in out:
        out["account_number"] = [
            p for p in out["account_number"]
            if p["group"] or "Xx*•" in p["regex"].pattern]
    return out


def lint_config(cfg: dict) -> list:
    """Warn about config keys the tool does not understand — a typo like
    'custom_term:' must never silently reduce redaction."""
    warnings = []
    known = {"preset", "categories", "custom_terms", "exclude_terms",
             "options", "custom_patterns", "passwords"}
    for k in cfg:
        if k not in known:
            warnings.append(f"unknown setting {k!r} is ignored "
                            f"(known: {', '.join(sorted(known))})")
    opts = cfg.get("options") or {}
    known_opts = {"ocr", "redact_barcodes", "output", "office_converter",
                  "handwriting_ocr", "redact_handwriting", "redact_faces"}
    for k in opts if isinstance(opts, dict) else ():
        if k not in known_opts:
            warnings.append(f"unknown option {k!r} is ignored")
    out = opts.get("output") if isinstance(opts, dict) else None
    for k in out if isinstance(out, dict) else ():
        if k not in {"images", "documents", "everything", "combine"}:
            warnings.append(f"unknown output setting {k!r} is ignored")
    return warnings


def run_convert_flow(handler, kind, args, input_path, preset, categories,
                     exclude, opts, use_ocr, redact_barcodes, dry_run):
    """Kind A handlers (images, docx, pptx): convert to PDF, redact through
    the proven PDF pipeline, optionally render images back to their
    original format. Returns the process exit code."""
    try:
        pdf_bytes, info = handler.to_pdf(input_path, opts)
    except UnsupportedFormatError as e:
        print(f"Unsupported: {e}")
        return 4

    out_opts = opts.get("output") or {}
    everything_pdf = out_opts.get("everything") == "pdf"
    image_mode = out_opts.get("images", "original") if kind == "image" else None
    if kind == "image" and image_mode == "png":
        out_ext = ".png"
    elif kind == "image" and image_mode != "pdf" and not everything_pdf:
        out_ext = input_path.suffix.lower()
    else:
        out_ext = ".pdf"
    output_path, report_path = resolve_outputs(args, input_path, out_ext)

    dropped = info.get("dropped_elements", 0)
    print(f"Format  : {kind} → PDF via {info.get('converter', '?')}"
          + (f"; {dropped} element(s) could not be converted" if dropped else ""))

    work_pdf = output_path.with_name("." + output_path.name + ".work.pdf")
    redacted_pdf = (output_path if out_ext == ".pdf" else
                    output_path.with_name("." + output_path.name + ".redacted.pdf"))
    work_pdf.write_bytes(pdf_bytes)
    try:
        results = process_pdf(work_pdf, redacted_pdf, categories,
                              dry_run=dry_run, redact_barcodes=redact_barcodes,
                              exclude=exclude, use_ocr=use_ocr,
                              vision_opts=opts)
        if results["total_text_chars"] == 0:
            if kind == "image":
                # A photo with no readable text is normal — barcodes and
                # metadata are still handled; just say so.
                info.setdefault("notes", []).append(
                    "no readable text found in the image (OCR)")
            else:
                print("\n*** Conversion produced NO text — the document "
                      "could not be read. NOTHING was redacted. ***")
                if not dry_run and redacted_pdf.exists():
                    redacted_pdf.unlink()
                return 2

        remaining = None
        if not dry_run:
            remaining = verify_output(redacted_pdf, categories,
                                      ocr_pages=set(results["ocr_pages"]),
                                      exclude=exclude,
                                      vision_pages=set(results.get("vision_pages", ())),
                                      vision_opts=opts)
            if kind == "image" and out_ext != ".pdf":
                doc = fitz.open(redacted_pdf)
                output_path = handler.write_back(doc, input_path,
                                                 output_path, opts)
                doc.close()

        write_report(report_path, input_path, output_path, preset,
                     results, remaining, dry_run, conversion=info)

        total = sum(results["counts"].values())
        print(f"\nMatches : {total} across {len(results['counts'])} categories")
        for note in info.get("notes", []):
            print(f"Note    : {note}")
        if not dry_run:
            print(f"Output  : {output_path}")
            print("Verify  : PASS — output re-scanned, 0 remaining matches."
                  if remaining == {} else
                  "Verify  : *** FAIL — sensitive text remains in output. "
                  "See report. Do not share the file. ***")
        print(f"Report  : {report_path}")
        if remaining:
            return 3
        if results["scanned_pages"]:
            return 2
        return 0
    finally:
        work_pdf.unlink(missing_ok=True)
        if redacted_pdf != output_path:
            redacted_pdf.unlink(missing_ok=True)


def run_native_flow(handler, kind, args, input_path, preset, categories,
                    exclude, opts, dry_run):
    """Kind B handlers (text, csv, xlsx): the handler redacts natively in
    its own format and verifies its own output. Returns the exit code."""
    cats = (tabular_categories(categories) if kind in ("csv", "excel")
            else categories)

    def matcher(text):
        return find_matches_in_text(text, cats, exclude)

    output_path, report_path = resolve_outputs(
        args, input_path, input_path.suffix.lower())
    print(f"Format  : {kind} (native redaction, {input_path.suffix} in/out)")
    try:
        results = handler.redact_file(input_path, output_path, matcher,
                                      dry_run, opts)
    except UnsupportedFormatError as e:
        print(f"Unsupported: {e}")
        return 4
    remaining = None
    if not dry_run:
        remaining = handler.verify_file(output_path, matcher, opts)
        if remaining == {} and (opts.get("output") or {}).get("everything") == "pdf":
            from handlers.pdf_render import render_to_pdf_bytes
            # Same disambiguation as resolve_outputs(): insert the original
            # extension so a same-stem .pdf input can't collide with this.
            orig_ext = input_path.suffix.lower().lstrip(".")
            pdf_path = output_path.with_name(
                f"{input_path.stem}_{orig_ext}_redacted.pdf")
            pdf_path.write_bytes(render_to_pdf_bytes(output_path))
            output_path.unlink()
            output_path = pdf_path
            # Report name must track the renamed output (matches
            # resolve_outputs()'s convention and combine_outputs.py's
            # lookup — otherwise combine would find no report for this
            # file and refuse to merge it even though it's clean). The
            # report itself isn't written until write_native_report()
            # below, so there's nothing to rename on disk here yet.
            # An explicit --report path is left exactly as the user gave it.
            if not args.report:
                report_path = output_path.with_name(
                    output_path.name + "_report.txt")
            results.setdefault("notes", []).append(
                "output converted to PDF (options.output.everything: pdf)")
    write_native_report(report_path, input_path, output_path, preset,
                        results, remaining, dry_run)
    total = sum(results["counts"].values())
    print(f"\nMatches : {total} across {len(results['counts'])} categories")
    for note in results.get("notes", []):
        print(f"Note    : {note}")
    if not dry_run:
        print(f"Output  : {output_path}")
        print("Verify  : PASS — output re-scanned, 0 remaining matches."
              if remaining == {} else
              "Verify  : *** FAIL — sensitive text remains in output. "
              "See report. Do not share the file. ***")
    print(f"Report  : {report_path}")
    return 3 if remaining else 0


def write_native_report(report_path, input_path, output_path, preset,
                        results, remaining, dry_run):
    """Report for natively redacted formats (text/csv/xlsx)."""
    lines = []
    add = lines.append
    bar = "=" * 70
    add(bar)
    add("REDACTION " + ("DRY-RUN (preview) " if dry_run else "") + "REPORT")
    add(bar)
    add(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)")
    add(f"Input     : {input_path}")
    add(f"Output    : {'(none — dry run)' if dry_run else output_path}")
    add(f"Preset    : {preset}")
    add(f"Scanned   : {results.get('unit_count', '?')} "
        f"{results.get('unit_label', 'unit')}(s)")
    add("")
    add("-" * 70)
    add("MATCHES BY CATEGORY")
    add("-" * 70)
    if results["counts"]:
        for cat in sorted(results["counts"]):
            add(f"  {CATEGORY_LABELS.get(cat, cat):<32} "
                f"{results['counts'][cat]:>4} match(es)")
    else:
        add("  No matches found for the enabled categories.")
    add("")
    add("-" * 70)
    add(f"DETAIL ({results.get('unit_label', 'unit')} / category / matched text)")
    add("-" * 70)
    for unit, cat, matched, _ in results.get("matches", []):
        shown = (" ".join(str(matched).split()) if dry_run
                 else mask_value(str(matched)))
        add(f"  {unit:<14} {CATEGORY_LABELS.get(cat, cat):<28} {shown}")
    if not results.get("matches"):
        add("  (none)")
    add("")
    if results.get("notes"):
        add("-" * 70)
        add("NOTES")
        add("-" * 70)
        for note in results["notes"]:
            for i, line in enumerate(_wrap(str(note), 66)):
                add(("  - " if i == 0 else "    ") + line)
        add("")
    add("-" * 70)
    add("POST-REDACTION VERIFICATION")
    add("-" * 70)
    if dry_run:
        add("  Skipped (dry run — no output file was created).")
    elif not remaining:
        add("  PASS — the output was re-opened and re-scanned with the same")
        add("  patterns: 0 remaining matches.")
    else:
        add("  *** FAIL — re-scan of the OUTPUT still finds matches: ***")
        for cat, n in sorted(remaining.items()):
            add(f"    {CATEGORY_LABELS.get(cat, cat)}: {n}")
        add("  Do NOT share this file. Review it manually.")
    add("")
    add("Reminder: automated redaction is a first pass, not a guarantee.")
    add(bar)
    report_path.write_text("\n".join(lines), encoding="utf-8")


# Exit-code severity order for aggregating an email + its attachments into
# one process exit code: verify-FAIL outranks unreadable outranks
# unsupported outranks success.
_EXIT_SEVERITY = {0: 0, 4: 1, 2: 2, 3: 3}
_MAX_EMAIL_DEPTH = 2  # an email attached to an email attached to an email...


def run_email_flow(handler, args, input_path, preset, categories, exclude,
                   opts, use_ocr, redact_barcodes, dry_run, depth=0):
    """Kind A body conversion (via the normal convert flow) PLUS recursive
    redaction of attachments, each as its own independently-verified output
    (docs/plans/expansion-plan.md §3.E). Returns the worst exit code across
    the body and every attachment."""
    body_rc = run_convert_flow(handler, "email", args, input_path, preset,
                               categories, exclude, opts, use_ocr,
                               redact_barcodes, dry_run)
    if dry_run:
        return body_rc  # attachments are not previewed in dry-run mode

    try:
        attachments = handler.extract_attachments(input_path)
    except Exception as e:
        print(f"Note    : attachments not extracted: {e}")
        return body_rc
    if not attachments:
        return body_rc

    out_dir = Path(args.output).expanduser() if args.output else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    worst_rc = body_rc
    att_args = argparse.Namespace(output=str(out_dir), report=None)

    with tempfile.TemporaryDirectory(prefix="ai-redact-email-att-") as tmp:
        tmp_dir = Path(tmp)
        for i, (name, blob, skip_reason) in enumerate(attachments, 1):
            print(f"\n--- Attachment {i}/{len(attachments)}: {name} ---")
            if skip_reason:
                print(f"Skipped : {skip_reason}")
                continue
            if depth >= _MAX_EMAIL_DEPTH:
                print(f"Skipped : max email-attachment recursion depth "
                      f"({_MAX_EMAIL_DEPTH}) reached")
                continue
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or f"attachment{i}"
            # Naming the temp file AS the desired final stem lets the
            # existing resolve_outputs()/*_flow machinery produce exactly
            # "<email-stem>_att{i}_<name>_redacted.<ext>" with no special
            # casing — it just sees another input file with that name.
            att_path = tmp_dir / f"{stem}_att{i}_{safe_name}"
            att_path.write_bytes(blob)

            kind2, handler2 = route_input(att_path)
            if kind2 == "unsupported":
                print(f"Unsupported: attachment {name!r} is not a "
                      f"supported format")
                worst_rc = max(worst_rc, 4, key=_EXIT_SEVERITY.get)
                continue
            try:
                if kind2 in ("office", "office_binary"):
                    handler2 = resolve_office_handler(
                        att_path.suffix.lower(), opts)
                if kind2 == "email":
                    rc2 = run_email_flow(handler2, att_args, att_path, preset,
                                         categories, exclude, opts, use_ocr,
                                         redact_barcodes, dry_run=False,
                                         depth=depth + 1)
                elif kind2 in ("image", "office", "office_binary",
                              "legacy_office", "epub"):
                    rc2 = run_convert_flow(handler2, kind2, att_args, att_path,
                                           preset, categories, exclude, opts,
                                           use_ocr, redact_barcodes, False)
                elif kind2 in ("text", "csv", "excel"):
                    rc2 = run_native_flow(handler2, kind2, att_args, att_path,
                                          preset, categories, exclude, opts,
                                          False)
                elif kind2 == "pdf":
                    out_path, rep_path = resolve_outputs(att_args, att_path, ".pdf")
                    results2 = process_pdf(att_path, out_path, categories,
                                           dry_run=False,
                                           redact_barcodes=redact_barcodes,
                                           exclude=exclude, use_ocr=use_ocr,
                                           vision_opts=opts)
                    remaining2 = verify_output(
                        out_path, categories,
                        ocr_pages=set(results2["ocr_pages"]), exclude=exclude,
                        vision_pages=set(results2.get("vision_pages", ())),
                        vision_opts=opts)
                    write_report(rep_path, att_path, out_path, preset,
                                results2, remaining2, False)
                    rc2 = (3 if remaining2 else
                           2 if results2["scanned_pages"] else 0)
                else:
                    rc2 = 4
            except UnsupportedFormatError as e:
                print(f"Unsupported: {e}")
                rc2 = 4
            worst_rc = max(worst_rc, rc2, key=_EXIT_SEVERITY.get)
    return worst_rc


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
    parser.add_argument("input", nargs="?",
                        help="File to redact: PDF, image (jpg/png/tiff/…), "
                             "docx, pptx, xlsx, doc/odt/rtf, csv/tsv, or text")
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
    parser.add_argument("--password",
                        help="Password for an encrypted PDF/docx/xlsx/pptx "
                             "(one-off; batch runs can instead use the "
                             "config's passwords: filename map)")
    parser.add_argument("--list-categories", action="store_true",
                        help="List available categories and presets, then exit")
    parser.add_argument("--check-config", action="store_true",
                        help="Print exactly what the config resolves to "
                             "(terms, switches, options), then exit")
    args = parser.parse_args()

    if args.list_categories:
        print("Categories:")
        for cat in CATEGORY_PATTERNS:
            print(f"  {cat:<16} {CATEGORY_LABELS[cat]}")
        print("\nPresets:")
        for name, cats in PRESETS.items():
            print(f"  {name:<10} {', '.join(cats)}")
        return

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
    for w in lint_config(cfg):
        print(f"  ! Config: {w}")

    # Preset/categories/exclude: shared with combine_outputs.py so the
    # combined PDF's re-verify pass matches the per-file run exactly.
    preset, categories, exclude, opts, forced_on, forced_off, custom = (
        resolve_categories(cfg, args.preset, args.categories))

    # Behavior toggles from the config's options: section.
    use_ocr = bool(opts.get("ocr", True))
    redact_barcodes = bool(opts.get("redact_barcodes", True))

    if args.check_config:
        print(f"Config    : {config_path}"
              + ("" if config_path.exists() else "  (NOT FOUND)"))
        print(f"Preset    : {preset}")
        print(f"Categories: {', '.join(categories) or '(none)'}")
        if forced_on:
            print(f"Always on : {', '.join(forced_on)}")
        if forced_off:
            print(f"Never     : {', '.join(forced_off)}")
        terms = list(flatten_terms(cfg.get("custom_terms")))
        print(f"Custom terms ({len(terms)}):")
        for t in terms:
            print(f"  - {t}")
        print(f"Exclude terms ({len(exclude)}):")
        for t in exclude:
            print(f"  - {t}")
        # Passwords configured: count only — NEVER print the values here.
        # This is a review-time audit command; leaking secrets into its
        # output would defeat the point (expansion-plan.md §2a.3).
        passwords = cfg.get("passwords") or {}
        print(f"Passwords configured: "
              f"{len(passwords) if isinstance(passwords, dict) else 0} file(s)")
        print(f"Options   : ocr={use_ocr}, redact_barcodes={redact_barcodes}, "
              f"output.images="
              f"{(opts.get('output') or {}).get('images', 'original')}")
        return

    if not args.input:
        parser.error("input file is required "
                     "(or use --list-categories / --check-config)")
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")
    password = resolve_password(input_path, args.password, cfg)

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

    # Route by sniffed format: PDFs continue below; everything else goes
    # through its handler (convert-to-PDF or native redaction).
    kind, handler = route_input(input_path)
    if kind == "unsupported":
        print(f"Unsupported file type: {input_path.name}\n"
              f"Supported: PDF, images (jpg/png/tiff/heic/…), docx, pptx, "
              f"xlsx, doc/odt/rtf, eml/msg, epub, csv/tsv, and text files.\n"
              f"(.xls/.ppt/.odp need LibreOffice — see docs/CONFIGURATION.md.)\n"
              f"Export the document to one of those and re-run.")
        sys.exit(4)
    if kind in ("office", "office_binary"):
        try:
            handler = resolve_office_handler(input_path.suffix.lower(), opts)
        except UnsupportedFormatError as e:
            print(f"Unsupported: {e}")
            sys.exit(4)
    if kind == "encrypted_office":
        sys.exit(run_decrypted_office_flow(input_path, password, args, preset,
                                           categories, exclude, opts, use_ocr,
                                           redact_barcodes, args.dry_run))
    if kind == "email":
        sys.exit(run_email_flow(handler, args, input_path, preset,
                                categories, exclude, opts, use_ocr,
                                redact_barcodes, args.dry_run))
    if kind in ("image", "office", "office_binary", "legacy_office", "epub"):
        sys.exit(run_convert_flow(handler, kind, args, input_path, preset,
                                  categories, exclude, opts, use_ocr,
                                  redact_barcodes, args.dry_run))
    if kind in ("text", "csv", "excel"):
        sys.exit(run_native_flow(handler, kind, args, input_path, preset,
                                 categories, exclude, opts, args.dry_run))

    output_path, report_path = resolve_outputs(args, input_path, ".pdf")
    try:
        results = process_pdf(input_path, output_path, categories,
                              dry_run=args.dry_run,
                              redact_barcodes=redact_barcodes,
                              exclude=exclude, use_ocr=use_ocr,
                              password=password, vision_opts=opts)
    except EncryptedFileError as e:
        print(f"Encrypted: {e}")
        sys.exit(5)

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
                                  exclude=exclude,
                                  vision_pages=set(results.get("vision_pages", ())),
                                  vision_opts=opts)

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
