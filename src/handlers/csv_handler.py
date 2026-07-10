"""Native (Kind B) handler for delimited files (.csv, .tsv).

Parses with the csv module (so quoted delimiters and embedded newlines in
cells are handled correctly), runs the matcher on each CELL individually,
and replaces every matched span inside the cell. Row 1 is treated as the
header row: each data cell is ALSO scanned as "Header: value" so that
label-based detectors fire on labeled columns (an "SSN" column full of
unformatted 9-digit values would otherwise never match), but replacements
only apply to matched text that occurs inside the cell value itself.
Rows are re-written with csv.writer's default quoting rules — cell
CONTENT is preserved exactly; quoting style may be normalized.

Contract: docs/plans/handler-spec.md (Kind B).
"""

from __future__ import annotations

import csv
import io
import pathlib
import sys

try:
    from handlers.common import UnsupportedFormatError, redaction_text
except ImportError:  # direct execution (smoke test): put src/ on the path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from handlers.common import UnsupportedFormatError, redaction_text

SUPPORTED_EXTENSIONS = {".csv", ".tsv"}


def _delimiter_for(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"csv_handler does not support '{ext}' files "
            f"(supported: .csv, .tsv).")
    return "\t" if ext == ".tsv" else ","


def _read_rows(path: pathlib.Path, delimiter: str) -> tuple[list, list]:
    """Parse all rows. UTF-8 with a latin-1 fallback (noted; the redacted
    output is always written as UTF-8 since the redaction block character
    does not exist in latin-1)."""
    data = path.read_bytes()
    notes = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        notes.append(f"{path.name}: not valid UTF-8 — decoded as latin-1; "
                     "output written as UTF-8 (non-ASCII bytes re-encoded)")
    # StringIO does no newline translation, which is what csv.reader needs
    # to see embedded newlines inside quoted cells intact.
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return rows, notes


def _cell_matches(cell: str, header: str, matcher) -> list:
    """Matches to redact within one cell, longest first.

    Scans the bare cell (self-contained matches like emails) AND, for data
    cells, "Header: value" — giving label-based contextual detectors their
    label. Only hits whose matched text occurs inside the cell value are
    kept; a hit that falls in the header prefix cannot be applied here
    (and would re-fire on every row of the column).
    """
    found = dict.fromkeys(matcher(cell))
    if header:
        for hit in matcher(f"{header}: {cell}"):
            if hit not in found and hit[1] in cell:
                found[hit] = None
    return sorted(found, key=lambda cs: -len(cs[1]))


def redact_file(input_path, output_path, matcher, dry_run: bool,
                options: dict) -> dict:
    """Scan every cell (and unless dry_run, write the redacted copy)."""
    input_path = pathlib.Path(input_path)
    delimiter = _delimiter_for(input_path)
    rows, notes = _read_rows(input_path, delimiter)
    headers = rows[0] if rows else []
    if len(rows) > 1:
        notes.append("header-context scanning active: row 1 is treated as "
                     "column labels so label-based detectors (SSN, account "
                     "no., ...) apply to labeled columns")

    results = {
        "counts": {},
        "matches": [],
        "unit_label": "cell",
        "unit_count": 0,
        "notes": notes,
    }

    out_rows = []
    for r, row in enumerate(rows, 1):
        out_row = []
        for c, cell in enumerate(row, 1):
            results["unit_count"] += 1
            header = headers[c - 1] if r > 1 and c <= len(headers) else ""
            # Longest matches first, so substring hits never leave
            # fragments. Replace ALL occurrences in the cell.
            for category, matched in _cell_matches(cell, header, matcher):
                n = cell.count(matched)
                if n == 0:
                    continue  # subsumed by a longer, already-redacted match
                results["counts"][category] = (
                    results["counts"].get(category, 0) + n)
                results["matches"].append(
                    (f"row {r} col {c}", category, matched, True))
                cell = cell.replace(matched, redaction_text(matched))
            out_row.append(cell)
        out_rows.append(out_row)

    if not dry_run:
        # newline="" per the csv docs, so the writer controls line endings
        # and embedded newlines inside quoted cells survive round-trip.
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter=delimiter).writerows(out_rows)
    return results


def verify_file(output_path, matcher, options: dict) -> dict:
    """Re-parse the written output per cell and re-run the matcher, using
    the same header-context construction as redact_file.

    Returns {category: remaining_count} — MUST be {} on success."""
    output_path = pathlib.Path(output_path)
    rows, _ = _read_rows(output_path, _delimiter_for(output_path))
    headers = rows[0] if rows else []
    remaining = {}
    for r, row in enumerate(rows, 1):
        for c, cell in enumerate(row, 1):
            header = headers[c - 1] if r > 1 and c <= len(headers) else ""
            for category, _matched in _cell_matches(cell, header, matcher):
                remaining[category] = remaining.get(category, 0) + 1
    return remaining


# ---------------------------------------------------------------------------
# Standalone smoke test (see handler-spec.md): trivial email matcher only.
# ---------------------------------------------------------------------------
def _smoke_test() -> int:
    import re
    import tempfile

    email_re = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

    def matcher(text):
        hits = dict.fromkeys(("email", m.group(0))
                             for m in email_re.finditer(text))
        return sorted(hits, key=lambda cs: -len(cs[1]))

    planted = "planted.email@example.com"
    rows = [
        ["name", "email", "address", "balance"],
        ["Plantedname, Casey", planted, "123 Fake St\nSpringfield",
         "$12,345.67"],
    ]

    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "sample.csv"
        dst = pathlib.Path(td) / "redacted_sample.csv"
        with open(src, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)

        dry = redact_file(src, dst, matcher, dry_run=True, options={})
        if dst.exists():
            print("FAIL: dry_run wrote an output file")
            return 1
        if dry["counts"].get("email", 0) != 1:
            print(f"FAIL: dry_run counts wrong: {dry['counts']}")
            return 1

        res = redact_file(src, dst, matcher, dry_run=False, options={})
        out = dst.read_text(encoding="utf-8")
        if planted in out:
            print("FAIL: planted email survived redaction")
            return 1
        if "$12,345.67" not in out:
            print("FAIL: financial string was not preserved")
            return 1
        out_rows = list(csv.reader(io.StringIO(out)))
        if out_rows[1][0] != "Plantedname, Casey":
            print("FAIL: quoted cell with comma was corrupted")
            return 1
        if out_rows[1][2] != "123 Fake St\nSpringfield":
            print("FAIL: embedded newline in cell was corrupted")
            return 1
        if res["unit_label"] != "cell" or res["unit_count"] != 8:
            print(f"FAIL: bad result schema: {res}")
            return 1
        if res["matches"][0][0] != "row 2 col 2":
            print(f"FAIL: bad match label: {res['matches']}")
            return 1
        if verify_file(dst, matcher, {}) != {}:
            print("FAIL: verify_file found remaining matches")
            return 1
        if not any("header-context" in n for n in res["notes"]):
            print(f"FAIL: header-context note missing: {res['notes']}")
            return 1
        print(f"ok: sample.csv: {res['counts']} across "
              f"{res['unit_count']} cells; verify clean")

        # Header-as-context: an unformatted 9-digit value in an "SSN"
        # column only matches when the column header supplies the label.
        ssn_ctx = re.compile(r"(?i)\bssn\b\s*:?\s*(?P<redact>\d{9})\b")

        def ssn_matcher(text):
            hits = dict.fromkeys(("ssn", m.group("redact"))
                                 for m in ssn_ctx.finditer(text))
            return sorted(hits, key=lambda cs: -len(cs[1]))

        src2 = pathlib.Path(td) / "labeled.csv"
        dst2 = pathlib.Path(td) / "redacted_labeled.csv"
        with open(src2, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows([["name", "SSN"], ["Casey", "000554444"]])
        res2 = redact_file(src2, dst2, ssn_matcher, dry_run=False, options={})
        out2 = list(csv.reader(io.StringIO(dst2.read_text(encoding="utf-8"))))
        if "000554444" in dst2.read_text(encoding="utf-8"):
            print("FAIL: labeled-column SSN survived (header context broken)")
            return 1
        if out2[0][1] != "SSN":
            print("FAIL: header cell was corrupted")
            return 1
        if res2["counts"] != {"ssn": 1} or res2["matches"][0][0] != "row 2 col 2":
            print(f"FAIL: bad labeled-column result: {res2}")
            return 1
        if verify_file(dst2, ssn_matcher, {}) != {}:
            print("FAIL: verify_file found remaining labeled-column matches")
            return 1
        print(f"ok: labeled.csv: {res2['counts']} via header context; "
              f"verify clean")
    print("csv_handler smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
