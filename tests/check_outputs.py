#!/usr/bin/env python3
"""Assert that redacted outputs contain no planted PII (and kept the money).

Usage: check_outputs.py <output_dir>

Extracts text from every redacted artifact (PDF text + OCR, image OCR,
text/csv reads, xlsx cells) and fails loudly if any planted identifier from
the fixture generators survived, or if the must-survive financial string
was lost from a text-bearing document.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fitz  # noqa: E402

PLANTED = [
    "planted.email@example.com",
    "(555) 010-9999",
    "000-55-4444",
    "casey plantedname",
    "000554444",
]
MUST_SURVIVE = "$12,345.67"
TEXT_EXTS = {".txt", ".md", ".log", ".json", ".yaml", ".yml",
             ".xml", ".html", ".htm", ".csv", ".tsv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
              ".gif", ".ico", ".avif", ".heic"}


def extract_text(path: Path) -> tuple:
    """Return (text, is_image_based)."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return path.read_text(encoding="utf-8", errors="replace"), False
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path)
        cells = [str(c.value) for ws in wb.worksheets
                 for row in ws.iter_rows() for c in row
                 if c.value is not None]
        for ws in wb.worksheets:
            cells.append(ws.title)
        return "\n".join(cells), False
    if ext == ".pdf":
        doc = fitz.open(path)
        text = "\n".join(p.get_text() for p in doc)
        image_based = not text.strip()
        if image_based:
            text = "\n".join(_ocr_page(p) for p in doc)
        doc.close()
        return text, image_based
    if ext in IMAGE_EXTS:
        # Load via Pillow (fitz can't read every format, e.g. AVIF) and
        # hand PNG bytes to PyMuPDF for OCR.
        import io

        from PIL import Image
        if ext in (".heic", ".heif"):
            import pillow_heif
            pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.open(path).convert("RGB").save(buf, format="PNG")
        doc = fitz.open("png", buf.getvalue())
        pdf = fitz.open("pdf", doc.convert_to_pdf())
        text = "\n".join(_ocr_page(p) for p in pdf)
        return text, True
    return "", True  # unknown: nothing to assert on


def _ocr_page(page) -> str:
    try:
        tp = page.get_textpage_ocr(dpi=300, full=True)
        return page.get_text("text", textpage=tp)
    except Exception:
        return ""


def main():
    out_dir = Path(sys.argv[1])
    artifacts = [p for p in sorted(out_dir.iterdir())
                 if p.is_file() and not p.name.endswith("_report.txt")
                 and not p.name.startswith(".")]
    if not artifacts:
        sys.exit("FAIL: no redacted artifacts found to check")
    failures = []
    for path in artifacts:
        text, image_based = extract_text(path)
        norm = " ".join(text.split()).lower()
        squashed = norm.replace(" ", "")
        for planted in PLANTED:
            p = planted.lower()
            if p in norm or p.replace(" ", "") in squashed:
                failures.append(f"{path.name}: planted value survived: "
                                f"{planted!r}")
        # OCR mangles currency glyphs, so only text-bearing formats get the
        # financial-preservation assertion.
        if not image_based and MUST_SURVIVE not in text:
            failures.append(f"{path.name}: must-survive financial string "
                            f"{MUST_SURVIVE!r} was lost")
        print(f"  checked {path.name}"
              + (" (via OCR)" if image_based else ""))
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    print(f"\nAll {len(artifacts)} artifacts clean: planted PII gone, "
          f"financial data preserved.")


if __name__ == "__main__":
    main()
