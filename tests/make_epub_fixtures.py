#!/usr/bin/env python3
"""
make_epub_fixtures.py — generate a fake-PII EPUB fixture for handler
tests (docs/plans/expansion-plan.md §3.F).

Writes into the directory given as argv[1]:
  fixture.epub — two spine chapters, entity-encoded PII in chapter 2 to
  prove the html_to_text() entity-decode step actually runs.

Pure Python (zipfile + stdlib only), no network.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PLANTED_EMAIL = "planted.email@example.com"
PLANTED_PHONE = "(555) 010-9999"
PLANTED_SSN = "000-55-4444"
PLANTED_NAME = "Casey Plantedname"
SURVIVOR = "$12,345.67"

_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata/>
  <manifest>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

_CH1 = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter 1</h1>
<p>Prepared by {PLANTED_NAME}. Direct line {PLANTED_PHONE}.</p>
<p>Closing balance for the period: {SURVIVOR} (verified).</p>
</body></html>
"""

# Entity-encoded email to prove html_to_text() actually decodes entities
# rather than just stripping tags.
_CH2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter 2</h1>
<p>Taxpayer SSN on file: {PLANTED_SSN}</p>
<p>Email inquiries to planted.email&#64;example.com within 5 days.</p>
</body></html>
"""


def make_epub(out_dir: Path) -> Path:
    path = Path(out_dir) / "fixture.epub"
    with zipfile.ZipFile(path, "w") as zf:
        # 'mimetype' MUST be the first entry, stored (uncompressed).
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", _OPF)
        zf.writestr("OEBPS/chapter1.xhtml", _CH1)
        zf.writestr("OEBPS/chapter2.xhtml", _CH2)
    print(f"wrote {path}")
    return path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: make_epub_fixtures.py <output-dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    make_epub(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
