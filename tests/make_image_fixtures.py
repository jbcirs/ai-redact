#!/usr/bin/env python3
"""Generate fake-PII image fixtures (see docs/plans/handler-spec.md).

Usage:  python tests/make_image_fixtures.py OUTPUT_DIR

Writes one fixture per supported-and-locally-writable image format into
OUTPUT_DIR. Every fixture contains RENDERED TEXT (large black-on-white,
OCR-readable) of the planted values:

    email  planted.email@example.com
    phone  (555) 010-9999
    ssn    000-55-4444
    name   Casey Plantedname
    money  $12,345.67          (must SURVIVE redaction)

The JPEG additionally carries planted fake EXIF metadata including a
GPSInfo IFD, so metadata-stripping can be verified. Pure Python, no
network; optional formats (AVIF, HEIC) are skipped with a note when this
environment can't write them. RAW (.cr2/.nef/...) and PSD cannot be
generated in pure Python — those handler paths are best-effort untested.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import ExifTags, Image, ImageDraw, ImageFont, features

PLANTED = {
    "email": "planted.email@example.com",
    "phone": "(555) 010-9999",
    "ssn": "000-55-4444",
    "name": "Casey Plantedname",
    "money": "$12,345.67",
}

ALL_LINES = [
    f"Email: {PLANTED['email']}",
    f"Phone: {PLANTED['phone']}",
    f"SSN: {PLANTED['ssn']}",
    f"Name: {PLANTED['name']}",
    f"Amount: {PLANTED['money']}",
]


def text_image(lines: list[str], font_size: int = 40,
               width: int = 1100) -> Image.Image:
    """White RGB canvas with large black text — generous spacing for OCR."""
    font = ImageFont.load_default(size=font_size)
    line_h = int(font_size * 2.2)
    img = Image.new("RGB", (width, line_h * len(lines) + 2 * line_h), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((50, line_h + i * line_h), line, fill="black", font=font)
    return img


def fake_exif() -> Image.Exif:
    """EXIF block with camera identity and GPS coordinates to strip."""
    exif = Image.Exif()
    exif[0x010F] = "FakeCam Industries"          # Make
    exif[0x0110] = "FakeCam 3000"                # Model
    exif[0x0132] = "2024:01:02 03:04:05"         # DateTime
    exif[0x013B] = "Casey Plantedname"           # Artist
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[1] = "N"                                 # GPSLatitudeRef
    gps[2] = (40.0, 26.0, 46.0)                  # GPSLatitude
    gps[3] = "W"                                 # GPSLongitudeRef
    gps[4] = (79.0, 58.0, 56.0)                  # GPSLongitude
    return exif


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: make_image_fixtures.py OUTPUT_DIR", file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)

    base = text_image(ALL_LINES)

    # --- Core formats -----------------------------------------------------
    p = out / "sample.png"
    base.save(p)
    print(f"wrote {p}")

    p = out / "sample.jpg"
    base.save(p, quality=95, exif=fake_exif())
    print(f"wrote {p}  (with fake EXIF incl. GPSInfo)")

    p = out / "sample.webp"
    base.save(p)
    print(f"wrote {p}")

    p = out / "sample.bmp"
    base.save(p)
    print(f"wrote {p}")

    # 2-page TIFF, planted text on both pages.
    page1 = text_image(["TIFF page 1"] + ALL_LINES)
    page2 = text_image(["TIFF page 2"] + ALL_LINES)
    p = out / "sample.tif"
    page1.save(p, save_all=True, append_images=[page2])
    print(f"wrote {p}  (2 pages, planted text on both)")

    # 2-frame GIF; frame 1 carries the planted text (handler keeps frame 0).
    frame1 = text_image(["GIF frame 1"] + ALL_LINES)
    frame2 = text_image(["GIF frame 2 (dropped on flatten)"])
    p = out / "sample.gif"
    frame1.save(p, save_all=True, append_images=[frame2], duration=500, loop=0)
    print(f"wrote {p}  (2 frames)")

    # ICO caps at 256px — short lines, smaller font, still black-on-white.
    ico = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(ico)
    font = ImageFont.load_default(size=18)
    for i, line in enumerate([PLANTED["email"], PLANTED["phone"],
                              PLANTED["ssn"], PLANTED["name"],
                              PLANTED["money"]]):
        draw.text((8, 20 + i * 42), line, fill="black", font=font)
    p = out / "sample.ico"
    ico.save(p, sizes=[(256, 256)])
    print(f"wrote {p}")

    # --- Optional formats -------------------------------------------------
    if features.check("avif"):
        p = out / "sample.avif"
        base.save(p, quality=100)  # lossy AVIF softens digits below OCR
        print(f"wrote {p}")
    else:
        print("skipped sample.avif (this Pillow build cannot write AVIF)")

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        p = out / "sample.heic"
        base.save(p)
        print(f"wrote {p}")
    except Exception as exc:
        print(f"skipped sample.heic (pillow-heif unavailable/unwritable: {exc})")

    print("note: RAW (.cr2/.cr3/.nef/.arw/.dng) and PSD fixtures cannot be "
          "generated in pure Python — those handler paths are best-effort "
          "untested locally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
