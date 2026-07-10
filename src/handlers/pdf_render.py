"""pdf_render.py — shared text-to-PDF flow-and-wrap renderer.

Extracted from office_handler.py (docs/plans/expansion-plan.md §2a.2) so
every "render plain/simplified content into a PDF" workstream (Office,
email bodies, EPUB chapters, everything->PDF) shares one implementation
instead of reinventing pagination/wrapping.

Local only. No network. No subprocesses.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import fitz  # PyMuPDF

# US-Letter geometry for the simplified PDF.
PAGE_W = 612.0
PAGE_H = 792.0
MARGIN = 50.0
FONT = "helv"  # plain built-in font (Latin-1 repertoire)


class PdfFlow:
    """Flows text and images down US-Letter pages, breaking to a new page
    on overflow. Tracks the y baseline explicitly so no line is ever
    written below the bottom margin (i.e. no text lost to overflow)."""

    def __init__(self) -> None:
        self.doc = fitz.open()
        self.substituted_chars = 0
        self.page = None
        self.y = 0.0
        self.new_page()

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.y = MARGIN  # next baseline is placed at y + leading

    # -- text ---------------------------------------------------------------

    def _sanitize(self, text: str) -> str:
        """The plain base-14 font only covers Latin-1; substitute anything
        else with '?' and count it so the loss is reported, not silent."""
        text = text.replace("\t", "  ").replace("\r", "")
        cleaned = text.encode("latin-1", "replace").decode("latin-1")
        self.substituted_chars += sum(
            1 for a, b in zip(text, cleaned) if a != b
        )
        return cleaned

    def _wrap(self, text: str, size: float) -> list[str]:
        max_w = PAGE_W - 2 * MARGIN
        lines: list[str] = []
        for raw in text.split("\n"):
            raw = raw.rstrip()
            if not raw:
                lines.append("")
                continue
            cur = ""
            for word in raw.split(" "):
                # Hard-split words wider than the whole line.
                while (
                    fitz.get_text_length(word, fontname=FONT, fontsize=size)
                    > max_w
                ):
                    if cur:
                        lines.append(cur)
                        cur = ""
                    k = len(word)
                    while k > 1 and (
                        fitz.get_text_length(
                            word[:k], fontname=FONT, fontsize=size
                        )
                        > max_w
                    ):
                        k -= 1
                    lines.append(word[:k])
                    word = word[k:]
                cand = f"{cur} {word}" if cur else word
                if (
                    fitz.get_text_length(cand, fontname=FONT, fontsize=size)
                    <= max_w
                ):
                    cur = cand
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        return lines

    def add_text(self, text: str, size: float = 11.0) -> None:
        if not text:
            return
        text = self._sanitize(text)
        leading = size * 1.35
        for line in self._wrap(text, size):
            if self.y + leading > PAGE_H - MARGIN:
                self.new_page()
            self.y += leading
            if line:
                self.page.insert_text(
                    (MARGIN, self.y), line, fontsize=size, fontname=FONT
                )

    def add_gap(self, points: float = 6.0) -> None:
        self.y = min(self.y + points, PAGE_H - MARGIN)

    # -- images ---------------------------------------------------------------

    def add_image(self, blob: bytes) -> bool:
        """Insert an image scaled to fit the text width. Returns False when
        the image format cannot be rendered (caller counts it dropped)."""
        try:
            pix = fitz.Pixmap(blob)
            iw, ih = float(pix.width), float(pix.height)
            pix = None
            if iw <= 0 or ih <= 0:
                return False
        except Exception:
            return False
        avail_w = PAGE_W - 2 * MARGIN
        max_h = PAGE_H - 2 * MARGIN
        w = min(avail_w, iw)
        h = ih * (w / iw)
        if h > max_h:
            w *= max_h / h
            h = max_h
        if self.y + h > PAGE_H - MARGIN:
            self.new_page()
        rect = fitz.Rect(MARGIN, self.y, MARGIN + w, self.y + h)
        try:
            self.page.insert_image(rect, stream=blob)
        except Exception:
            return False
        self.y += h + 6
        return True

    def to_bytes(self) -> bytes:
        return self.doc.tobytes()


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Strip tags and decode entities — used by any Kind A handler that
    renders HTML-ish content into a PDF (email bodies, EPUB chapters).
    Uses stdlib html.unescape() (the same decoder text_handler.py's
    entity-scan relies on) rather than a hand-rolled entity table, so
    every named/numeric entity is handled, not just the common few."""
    text = _TAG_RE.sub(" ", markup)
    text = _ANY_TAG_RE.sub(" ", text)
    return html.unescape(text)


_TEXT_LIKE_EXTENSIONS = {".txt", ".md", ".log", ".json", ".yaml", ".yml",
                         ".xml", ".html", ".htm", ".csv", ".tsv"}
_RASTER_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif",
                      ".tiff", ".gif", ".ico"}
_HEIF_EXTENSIONS = {".heic", ".heif", ".avif"}


def render_to_pdf_bytes(path: Path) -> bytes:
    """Render an ALREADY-REDACTED output file to PDF bytes.

    Used by options.output.everything ("pdf" forces every native-format
    output to PDF) and combine_outputs.py (native outputs are converted
    to PDF for the merge only — see expansion-plan.md §3.H). The input
    must already be the redacted artifact; this never re-runs detection.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return path.read_bytes()

    flow = PdfFlow()
    if ext == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            flow.add_text(f"Sheet: {ws.title}", size=12.0)
            flow.add_gap(4)
            for row in ws.iter_rows():
                cells = ["" if c.value is None else str(c.value) for c in row]
                if any(cells):
                    flow.add_text(" | ".join(cells), size=9.0)
            flow.add_gap(10)
        return flow.to_bytes()

    if ext in _RASTER_EXTENSIONS or ext in _HEIF_EXTENSIONS:
        blob = path.read_bytes()
        if ext in _HEIF_EXTENSIONS:
            import io

            import pillow_heif
            from PIL import Image

            pillow_heif.register_heif_opener()
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, format="PNG")
            blob = buf.getvalue()
        flow.add_image(blob)
        return flow.to_bytes()

    # Text-like formats (also covers csv/tsv — a plain, readable rendering
    # is sufficient here; this is a post-redaction convenience view, not
    # the primary output).
    text = path.read_text(encoding="utf-8", errors="replace")
    flow.add_text(text, size=10.0)
    return flow.to_bytes()
