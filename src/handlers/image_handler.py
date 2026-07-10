"""Image handler (Kind A convert handler — see docs/plans/handler-spec.md).

Converts raster images to PDF so the router can run the proven PDF
redaction/verify pipeline, and writes the redacted result back to the
original raster format where possible.

Decisions (docs/plans/format-support-plan.md §3.3):
  - Animated GIFs are flattened to the first frame (noted in the report).
  - Multi-page TIFFs become one PDF page per frame and round-trip back to a
    multi-page TIFF.
  - ICO: only the largest embedded size is processed (noted).
  - HEIC/HEIF read via pillow-heif (lazy import); default output is JPEG
    for recipient compatibility.
  - RAW (.cr2 .cr3 .nef .arw .dng) read via rawpy (lazy import); RAW can
    never be written back → JPEG out.
  - PSD reads the flattened composite (hidden layers can hold PII invisible
    in the composite — flattening is the point); no PSD out → JPEG out.
  - Write-back ALWAYS strips metadata (EXIF/GPS/XMP): output pixels are
    rebuilt from the rendered PDF page, never copied from the input.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

try:
    from handlers.common import UnsupportedFormatError
except ImportError:  # running as src.handlers.image_handler or directly
    try:
        from .common import UnsupportedFormatError
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from common import UnsupportedFormatError

RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng"}
HEIF_EXTENSIONS = {".heic", ".heif"}

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".ico", ".psd",
} | RAW_EXTENSIONS

# Extensions Pillow can (unconditionally) write back to. AVIF is checked at
# runtime (Pillow 11+ needs libavif support compiled in). Everything else
# falls back to JPEG.
_WRITABLE = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF",
    ".ico": "ICO",
}


def _register_heif() -> None:
    """Lazily register the pillow-heif opener (optional dependency)."""
    try:
        import pillow_heif
    except ImportError:
        raise UnsupportedFormatError(
            "HEIC/HEIF support requires the 'pillow-heif' package. "
            "Run:  pip install pillow-heif"
        )
    pillow_heif.register_heif_opener()


def _load_raw(input_path: Path) -> Image.Image:
    """Decode a camera RAW file to a PIL image via rawpy (optional dep)."""
    try:
        import rawpy
    except ImportError:
        raise UnsupportedFormatError(
            f"RAW support ({input_path.suffix}) requires the 'rawpy' "
            "package. Run:  pip install rawpy"
        )
    try:
        with rawpy.imread(str(input_path)) as raw:
            rgb = raw.postprocess()
    except Exception as exc:
        raise UnsupportedFormatError(
            f"Could not decode RAW file {input_path.name}: {exc}. "
            "Export it as JPEG/TIFF from your photo software and retry."
        )
    return Image.fromarray(rgb)


def _load_frames(input_path: Path) -> tuple[list[Image.Image], dict]:
    """Open the input image and return (RGB frames, info dict).

    Multi-page TIFF returns one frame per page; everything else returns a
    single frame. GIF/ICO extras are dropped (counted + noted).
    """
    ext = input_path.suffix.lower()
    info = {"converter": "pillow", "dropped_elements": 0, "notes": []}

    if ext in RAW_EXTENSIONS:
        info["converter"] = "rawpy"
        return [_load_raw(input_path).convert("RGB")], info

    if ext in HEIF_EXTENSIONS:
        _register_heif()
        info["converter"] = "pillow-heif"

    try:
        img = Image.open(input_path)
    except Exception as exc:
        raise UnsupportedFormatError(
            f"Could not open image {input_path.name}: {exc}"
        )

    with img:
        if ext == ".gif":
            n = getattr(img, "n_frames", 1)
            img.seek(0)
            frames = [img.convert("RGB")]
            if n > 1:
                info["dropped_elements"] = n - 1
                info["notes"].append("animated GIF flattened to first frame")
            return frames, info

        if ext in (".tif", ".tiff"):
            n = getattr(img, "n_frames", 1)
            frames = []
            for i in range(n):
                img.seek(i)
                frames.append(img.convert("RGB"))
            if n > 1:
                info["notes"].append(f"multi-page TIFF: {n} pages converted")
            return frames, info

        if ext == ".ico":
            # Pillow opens the largest embedded size by default; the smaller
            # sizes are regenerated on write-back, so count them as dropped.
            sizes = img.info.get("sizes") or set()
            if len(sizes) > 1:
                info["dropped_elements"] = len(sizes) - 1
                info["notes"].append(
                    f"ICO: processed largest size {img.size[0]}x{img.size[1]} "
                    f"only ({len(sizes) - 1} smaller sizes dropped)"
                )
            return [img.convert("RGB")], info

        if ext == ".psd":
            info["notes"].append(
                "PSD flattened to composite (hidden layers discarded)"
            )
            return [img.convert("RGB")], info

        return [img.convert("RGB")], info


def to_pdf(input_path: Path, options: dict) -> tuple[bytes, dict]:
    """Convert an image file to PDF bytes (one page per frame).

    Pages are sized so 1 pixel == 1 point, i.e. a 72-dpi-equivalent page
    whose rect matches the original pixel dimensions exactly — write_back
    relies on this to restore the original pixel size.
    """
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"Image handler does not support '{ext}' files."
        )

    frames, info = _load_frames(input_path)

    doc = fitz.open()
    try:
        for frame in frames:
            w, h = frame.size
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            page = doc.new_page(width=w, height=h)
            page.insert_image(page.rect, stream=buf.getvalue())
        pdf_bytes = doc.tobytes()
    finally:
        doc.close()
    return pdf_bytes, info


def _original_size(input_path: Path) -> tuple[int, int] | None:
    """Best-effort pixel size of the original file (None if unreadable)."""
    ext = input_path.suffix.lower()
    if ext in RAW_EXTENSIONS:
        return None  # rawpy decode is expensive; page rect already matches
    try:
        if ext in HEIF_EXTENSIONS:
            _register_heif()
        with Image.open(input_path) as img:
            return img.size
    except Exception:
        return None


def _render_page(page, target_size: tuple[int, int] | None) -> Image.Image:
    """Render a PDF page to a fresh RGB PIL image (metadata-free by
    construction: pixels only, nothing copied from the input file)."""
    rect = page.rect
    if target_size and rect.width > 0 and rect.height > 0:
        zoom = max(target_size[0] / rect.width, target_size[1] / rect.height)
    else:
        zoom = 1.0  # to_pdf pages are 1pt == 1px, so identity restores size
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def write_back(pdf_doc, input_path, output_path, options) -> Path:
    """Render the redacted PDF back to the original raster format.

    HEIC/HEIF, RAW, PSD (and AVIF when this Pillow build can't write it)
    come back as JPEG — the returned path reflects the actual extension.
    All metadata (EXIF/GPS/XMP/IPTC) is stripped: output images are built
    from rendered pixels only.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    ext = input_path.suffix.lower()

    fmt = _WRITABLE.get(ext)
    out_ext = ext
    if fmt is None and ext == ".avif":
        from PIL import features
        if features.check("avif"):
            fmt = "AVIF"
    if fmt is None:
        # HEIC/HEIF, RAW, PSD, or unwritable AVIF → JPEG out.
        fmt, out_ext = "JPEG", ".jpg"
    if output_path.suffix.lower() != out_ext:
        output_path = output_path.with_suffix(out_ext)

    target = _original_size(input_path)
    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = 95

    if fmt == "TIFF" and len(pdf_doc) > 1:
        # Multi-page TIFF round-trips to multi-page TIFF.
        frames = [_render_page(p, target) for p in pdf_doc]
        frames[0].save(
            output_path, format="TIFF",
            save_all=True, append_images=frames[1:], **save_kwargs,
        )
        return output_path

    img = _render_page(pdf_doc[0], target)
    if fmt == "ICO" and max(img.size) > 256:
        img.thumbnail((256, 256))  # ICO caps at 256px per side
    img.save(output_path, format=fmt, **save_kwargs)
    return output_path


# ---------------------------------------------------------------------------
# Standalone smoke test (see handler-spec.md "Standalone self-test")
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    import tempfile

    from PIL import ImageDraw, ImageFont

    email = "planted.email@example.com"
    lines = [
        f"Email: {email}",
        "Phone: (555) 010-9999",
        "SSN: 000-55-4444",
        "Name: Casey Plantedname",
        "Amount: $12,345.67",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. Generate a PNG fixture with large rendered text.
        font = ImageFont.load_default(size=40)
        img = Image.new("RGB", (1100, 90 * len(lines) + 100), "white")
        draw = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            draw.text((60, 60 + i * 90), line, fill="black", font=font)
        fixture = tmp / "smoke.png"
        img.save(fixture)

        # 2. Convert to PDF and OCR it — the planted email must be readable.
        pdf_bytes, info = to_pdf(fixture, {})
        assert info["converter"] == "pillow", info
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        assert len(doc) == 1, f"expected 1 page, got {len(doc)}"

        tessdata = None
        try:
            tessdata = fitz.get_tessdata()
        except Exception:
            pass
        if not tessdata:
            tessdata = "/opt/homebrew/share/tessdata"
        page = doc[0]
        tp = page.get_textpage_ocr(tessdata=tessdata, dpi=300, full=True)
        ocr_text = page.get_text(textpage=tp)
        assert email in ocr_text, (
            f"planted email not found in OCR text:\n{ocr_text!r}"
        )
        print(f"[ok] to_pdf: OCR found planted email ({info})")

        # 3. Write-back round trip: PDF → PNG, no metadata, original size.
        out = write_back(doc, fixture, tmp / "smoke_out.png", {})
        doc.close()
        assert out.exists(), f"write_back did not create {out}"
        assert out.suffix == ".png", out
        with Image.open(out) as back:
            assert back.size == img.size, (back.size, img.size)
            assert not dict(back.getexif()), "EXIF survived write_back"
            assert "exif" not in back.info, "exif blob survived write_back"
        print(f"[ok] write_back: {out.name} written, size preserved, no EXIF")

    print("image_handler smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
