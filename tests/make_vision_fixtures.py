#!/usr/bin/env python3
"""
make_vision_fixtures.py — generate an Apple Vision handwriting fixture
for handler tests (docs/plans/expansion-plan.md §3.D).

Writes into the directory given as argv[1]:
  handwriting.png — planted PII rendered in a cursive system font
  (Snell Roundhand), for handwriting_ocr / redact_handwriting.

NOTE: there is no synthetic-face image that reliably triggers Apple
Vision's face detector (it's trained on real photos; simple drawn shapes
do not trigger it, confirmed live on this Mac), and no real photo is
available without network access. redact_faces is therefore integration-
tested (runs cleanly, no crash, correct plumbing) but NOT accuracy-tested
by this fixture suite — see docs/plans/expansion-plan.md §3.D execution
notes. Provide a real photo to validate detection quality before relying
on it for anything sensitive.

Pure Python (Pillow, already a required dep), no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLANTED_NAME = "Casey Plantedname"

_CURSIVE_FONT = "/System/Library/Fonts/Supplemental/SnellRoundhand.ttc"


def make_handwriting_png(out_dir: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    path = Path(out_dir) / "handwriting.png"
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_CURSIVE_FONT, 60)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 60), PLANTED_NAME, font=font, fill="black")
    img.save(path)
    print(f"wrote {path}")
    return path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: make_vision_fixtures.py <output-dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    make_handwriting_png(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
