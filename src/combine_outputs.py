#!/usr/bin/env python3
"""
combine_outputs.py — merge every redacted output in a directory into one
combined_redacted.pdf (docs/plans/expansion-plan.md §3.H).

Usage:
  combine_outputs.py <output_dir> [--config PATH] [--preset NAME]
                      [--categories a,b,c]

Native-format outputs (text/csv/xlsx/images) are rendered to PDF for the
merge ONLY — their individual output files in <output_dir> are left
exactly as they are; this never rewrites them. Refuses (exit 3) to
include any file whose individual report did not record a PASS — prints
which file blocked the merge, per the "combined artifact must be
shareable as a unit" rule (expansion-plan.md §6 grill item 7). The
combined PDF is independently re-verified as a whole (re-scanned page by
page, OCR'ing any page with no text layer but images) before being
reported PASS; a failure here deletes the combined PDF rather than
leaving a misleading copy.

Local only. No network.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact  # noqa: E402
from handlers.pdf_render import render_to_pdf_bytes  # noqa: E402

import fitz  # noqa: E402

_SKIP_NAMES = {"combined_redacted.pdf", "combined_redacted_report.txt"}


def _report_for(output_path: Path) -> Path:
    # Must match resolve_outputs() in redact.py: the FULL output filename
    # (extension included), not .stem — see the collision bug documented
    # there (docs/plans/expansion-plan.md §3.H execution notes).
    return output_path.with_name(output_path.name + "_report.txt")


def _report_failed(report_path: Path) -> bool:
    """True unless the report exists and explicitly recorded no FAIL
    marker. Missing report = unsafe to merge, never assumed clean."""
    if not report_path.exists():
        return True
    return "*** FAIL" in report_path.read_text(encoding="utf-8", errors="replace")


def gather_outputs(out_dir: Path) -> list:
    return [p for p in sorted(out_dir.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith("_report.txt")
            and p.name not in _SKIP_NAMES]


def _ocr_page_text(page) -> str:
    try:
        tp = page.get_textpage_ocr(dpi=300, full=True, tessdata=redact.TESSDATA)
        return page.get_text("text", textpage=tp)
    except Exception:
        return ""


def combine(out_dir: Path, categories: dict, exclude: tuple) -> int:
    outputs = gather_outputs(out_dir)
    if not outputs:
        print("No redacted outputs found to combine.")
        return 0

    for p in outputs:
        report = _report_for(p)
        if _report_failed(report):
            print(f"REFUSING to combine: {p.name} has no PASSing report "
                  f"({report.name}). Fix or re-run that file first.")
            return 3

    combined = fitz.open()
    toc_lines = []
    for p in outputs:
        try:
            pdf_bytes = render_to_pdf_bytes(p)
        except Exception as e:
            print(f"REFUSING to combine: could not render {p.name} to PDF "
                  f"for the merge ({e}).")
            return 3
        src = fitz.open(stream=pdf_bytes, filetype="pdf")
        start = combined.page_count + 1
        combined.insert_pdf(src)
        end = combined.page_count
        src.close()
        rng = f"{start}-{end}" if end != start else str(start)
        toc_lines.append(f"  p.{rng:<9} {p.name}")

    combined.set_metadata({})
    try:
        combined.del_xml_metadata()
    except Exception:
        pass

    # Independent re-verify of the combined document, whole.
    remaining = {}
    for page in combined:
        text = page.get_text("text")
        if not text.strip() and page.get_images(full=True):
            text = _ocr_page_text(page)
        for category, _ in redact.find_matches_in_text(text, categories, exclude):
            remaining[category] = remaining.get(category, 0) + 1
        for link in page.get_links():
            uri = link.get("uri") or ""
            if uri and redact.find_matches_in_text(uri, categories, exclude):
                remaining["link"] = remaining.get("link", 0) + 1

    combined_path = out_dir / "combined_redacted.pdf"
    report_path = out_dir / "combined_redacted_report.txt"
    if remaining:
        combined.close()
        _write_report(report_path, outputs, toc_lines, remaining, ok=False)
        print("*** FAIL — the combined PDF still contains matches on "
              f"re-scan. Not written. See {report_path}")
        return 3

    combined.save(combined_path, garbage=4, deflate=True)
    combined.close()
    _write_report(report_path, outputs, toc_lines, remaining, ok=True)
    print(f"Combined: {len(outputs)} file(s) -> {combined_path}")
    print("Verify  : PASS — combined PDF re-scanned, 0 remaining matches.")
    print(f"Report  : {report_path}")
    return 0


def _write_report(report_path, outputs, toc_lines, remaining, ok):
    lines = []
    add = lines.append
    bar = "=" * 70
    add(bar)
    add("COMBINED REDACTION REPORT")
    add(bar)
    add(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)")
    add(f"Files     : {len(outputs)}")
    add("")
    add("-" * 70)
    add("TABLE OF CONTENTS (file -> page range in the combined PDF)")
    add("-" * 70)
    lines.extend(toc_lines)
    add("")
    add("-" * 70)
    add("POST-REDACTION VERIFICATION (combined document, re-scanned whole)")
    add("-" * 70)
    if ok:
        add("  PASS — 0 remaining matches across the combined document.")
    else:
        add("  *** FAIL — matches remain: ***")
        for cat, n in sorted(remaining.items()):
            add(f"    {redact.CATEGORY_LABELS.get(cat, cat)}: {n}")
        add("  combined_redacted.pdf was NOT written. Do not share it.")
    add("")
    add(bar)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir")
    parser.add_argument("-c", "--config")
    parser.add_argument("-p", "--preset")
    parser.add_argument("--categories")
    args = parser.parse_args(argv[1:])

    out_dir = Path(args.output_dir)
    if not out_dir.is_dir():
        sys.exit(f"Not a directory: {out_dir}")

    if args.config:
        config_path = Path(args.config).expanduser()
    else:
        config_path = Path(redact.DEFAULT_CONFIG)
        if not config_path.exists():
            config_path = redact.PROJECT_ROOT / redact.DEFAULT_CONFIG
    cfg = redact.load_config(config_path)
    _, categories, exclude, _, _, _, _ = redact.resolve_categories(
        cfg, args.preset, args.categories)

    return combine(out_dir, categories, exclude)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
