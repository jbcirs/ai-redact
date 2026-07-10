#!/usr/bin/env python3
"""
epub_handler.py — Kind A convert handler for EPUB (docs/plans/
expansion-plan.md §3.F).

EPUB is a zip of XHTML chapters plus an OPF manifest/spine that gives
their reading order. This walks the spine in order, strips each
chapter's markup down to plain text (via the shared html_to_text(), the
same entity-decoding text_handler.py's HTML scan relies on) and renders
it through the shared pdf_render flow. No native-EPUB output exists —
repackaging the zip risks leaking metadata or an unscanned resource the
converter didn't walk, so output is always PDF.

DRM'd EPUBs (Adobe ADEPT / rights-managed) are refused outright: this
tool will not and cannot break DRM.

Local only. No network.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import fitz  # noqa: F401  (parity import; fails loud if PyMuPDF missing)
except ImportError:  # pragma: no cover
    sys.exit(
        "PyMuPDF is not installed.\n"
        "Run:  pip install -r requirements.txt"
    )

try:  # imported as part of the handlers package (normal router path)
    from handlers.common import UnsupportedFormatError
    from handlers.pdf_render import PdfFlow, html_to_text
except ImportError:  # executed directly: python src/handlers/epub_handler.py
    from common import UnsupportedFormatError
    from pdf_render import PdfFlow, html_to_text

SUPPORTED_EXTENSIONS = {".epub"}

_CONTAINER_PATH = "META-INF/container.xml"
_NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_DRM_MARKERS = ("META-INF/rights.xml", "META-INF/encryption.xml")


def _opf_path(zf: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(zf.read(_CONTAINER_PATH))
    except Exception as exc:
        raise UnsupportedFormatError(
            f"could not read {_CONTAINER_PATH} — not a valid EPUB"
        ) from exc
    rootfile = root.find(f".//{_NS_CONTAINER}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise UnsupportedFormatError(
            "EPUB container.xml has no rootfile — not a valid EPUB")
    return rootfile.get("full-path")


def _spine_chapter_paths(zf: zipfile.ZipFile, opf_path: str) -> list:
    opf_dir = "/".join(opf_path.split("/")[:-1])
    try:
        opf = ET.fromstring(zf.read(opf_path))
    except Exception as exc:
        raise UnsupportedFormatError(
            f"could not parse OPF manifest {opf_path}"
        ) from exc

    manifest = {}
    for item in opf.findall(f".//{_NS_OPF}manifest/{_NS_OPF}item"):
        manifest[item.get("id")] = item.get("href")

    paths = []
    for itemref in opf.findall(f".//{_NS_OPF}spine/{_NS_OPF}itemref"):
        idref = itemref.get("idref")
        href = manifest.get(idref)
        if not href:
            continue
        path = f"{opf_dir}/{href}" if opf_dir else href
        paths.append(path)
    return paths


def _chapter_text(zf: zipfile.ZipFile, path: str) -> str:
    try:
        markup = zf.read(path).decode("utf-8", "replace")
    except KeyError:
        return ""
    return html_to_text(markup)


def to_pdf(input_path: Path, options: dict) -> tuple[bytes, dict]:
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"epub_handler cannot convert '{ext}' files — supported: .epub."
        )

    try:
        zf = zipfile.ZipFile(input_path)
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormatError(
            f"{input_path.name} is not a valid EPUB/zip file"
        ) from exc

    with zf:
        names = set(zf.namelist())
        if any(marker in names for marker in _DRM_MARKERS):
            raise UnsupportedFormatError(
                "this EPUB is DRM-protected (rights/encryption metadata "
                "present) — this tool will not attempt to break DRM. "
                "Remove DRM with a tool you're licensed to use, or obtain "
                "a DRM-free copy, then re-run."
            )

        opf_path = _opf_path(zf)
        chapter_paths = _spine_chapter_paths(zf, opf_path)
        if not chapter_paths:
            raise UnsupportedFormatError(
                "EPUB spine is empty — no chapters to convert")

        flow = PdfFlow()
        dropped = 0
        chapters_rendered = 0
        for path in chapter_paths:
            text = _chapter_text(zf, path)
            if not text.strip():
                dropped += 1
                continue
            if chapters_rendered:
                flow.new_page()
            flow.add_text(text, size=11.0)
            chapters_rendered += 1

    if chapters_rendered == 0:
        raise UnsupportedFormatError(
            "no readable chapter text found in this EPUB's spine")

    notes = [f"{chapters_rendered} chapter(s) rendered from the spine, in "
             f"reading order; layout simplified to plain text"]
    if dropped:
        notes.append(f"{dropped} spine entr(y/ies) had no extractable text "
                     f"(counted as dropped)")
    info = {
        "converter": "tier1-epub",
        "dropped_elements": dropped,
        "notes": notes,
    }
    return flow.to_bytes(), info
