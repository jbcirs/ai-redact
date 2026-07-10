#!/usr/bin/env python3
"""
make_legacy_office_fixtures.py — generate fake-PII legacy Word-family
fixtures (.doc, .odt, .rtf) for handler tests.

Writes into the directory given as argv[1]:
  fixture.rtf, fixture.doc, fixture.odt — each holding the same planted
  identifiers used by every other fixture generator (see
  docs/plans/handler-spec.md), produced via macOS's local `textutil`
  (offline system binary — not a network call) from a plain-text source.

Pure local generation, no network; the only external tool used is the
same textutil binary the handler itself relies on.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PLANTED_EMAIL = "planted.email@example.com"
PLANTED_PHONE = "(555) 010-9999"
PLANTED_SSN = "000-55-4444"
PLANTED_NAME = "Casey Plantedname"
SURVIVOR = "$12,345.67"

_TEXTUTIL = "/usr/bin/textutil"

_SOURCE_TEXT = f"""Legacy Document Review

Prepared by {PLANTED_NAME}. Direct line {PLANTED_PHONE}.
Email inquiries to {PLANTED_EMAIL} within 5 days.
Taxpayer SSN on file: {PLANTED_SSN}
Closing balance for the period: {SURVIVOR} (verified).
"""


def _make_via_textutil(out_dir: Path, fmt: str) -> Path:
    src = Path(out_dir) / f"_legacy_source_{fmt}.txt"
    src.write_text(_SOURCE_TEXT, encoding="utf-8")
    dest = Path(out_dir) / f"fixture.{fmt}"
    proc = subprocess.run(
        [_TEXTUTIL, "-convert", fmt, "-output", str(dest), str(src)],
        capture_output=True, text=True, timeout=30,
    )
    src.unlink(missing_ok=True)
    if proc.returncode != 0 or not dest.exists():
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        sys.exit(f"textutil could not produce {fmt} fixture: {detail}")
    print(f"wrote {dest}")
    return dest


def make_rtf(out_dir: Path) -> Path:
    return _make_via_textutil(out_dir, "rtf")


def make_doc(out_dir: Path) -> Path:
    return _make_via_textutil(out_dir, "doc")


def make_odt(out_dir: Path) -> Path:
    return _make_via_textutil(out_dir, "odt")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: make_legacy_office_fixtures.py <output-dir>",
              file=sys.stderr)
        return 2
    if not Path(_TEXTUTIL).exists():
        print("SKIP: textutil not available (this suite is macOS-only)")
        return 0
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    make_rtf(out_dir)
    make_doc(out_dir)
    make_odt(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
