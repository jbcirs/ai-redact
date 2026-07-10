"""vision_helper.py — Apple Vision framework integration for handwriting
OCR and face detection (docs/plans/expansion-plan.md §3.D).

Fully local: Vision runs on-device, no network. Vision and Quartz are
imported lazily inside each function (not at module import time) so
nothing here requires them unless a caller actually enables
handwriting_ocr / redact_handwriting / redact_faces — all default off.

Coordinate systems (spiked live on this Mac before writing this file —
see docs/plans/expansion-plan.md §6 grill item 4 for why that mattered):
Vision returns normalized bounding boxes (0..1) with a BOTTOM-LEFT
origin; PyMuPDF pages use point units with a TOP-LEFT origin.
map_bbox_to_page_rect() converts between them.
"""

from __future__ import annotations

import io

try:
    from handlers.common import UnsupportedFormatError
except ImportError:  # executed directly
    from common import UnsupportedFormatError


def _require_vision():
    try:
        import Vision
        from Foundation import NSData
    except ImportError as exc:
        raise UnsupportedFormatError(
            "pyobjc-framework-Vision is not installed — run "
            "'pip install -r requirements.txt' to use handwriting_ocr / "
            "redact_handwriting / redact_faces."
        ) from exc
    return Vision, NSData


def _image_handler(png_bytes: bytes):
    Vision, NSData = _require_vision()
    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    return Vision, Vision.VNImageRequestHandler.alloc().initWithData_options_(
        data, None)


def detect_faces(png_bytes: bytes) -> list:
    """Returns a list of normalized (x, y, w, h) face bounding boxes,
    bottom-left origin, 0..1 range. Empty list if none found — never
    raises for "no faces" (that's the expected common case)."""
    Vision, handler = _image_handler(png_bytes)
    request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise UnsupportedFormatError(f"Vision face detection failed: {error}")
    boxes = []
    for obs in request.results() or []:
        bbox = obs.boundingBox()
        boxes.append((bbox.origin.x, bbox.origin.y,
                      bbox.size.width, bbox.size.height))
    return boxes


def recognize_text(png_bytes: bytes) -> list:
    """Returns [(text, (x, y, w, h)), ...] — one entry per Vision text
    observation (line-ish granularity), normalized bottom-left bboxes.
    Uses the accurate recognition level: slower, but Apple's docs say it
    is what handles handwriting/cursive reasonably, where the fast level
    does not."""
    Vision, handler = _image_handler(png_bytes)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)  # VNRequestTextRecognitionLevelAccurate
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise UnsupportedFormatError(f"Vision text recognition failed: {error}")
    out = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        text = str(candidates[0].string())
        if not text.strip():
            continue
        bbox = obs.boundingBox()
        out.append((text, (bbox.origin.x, bbox.origin.y,
                           bbox.size.width, bbox.size.height)))
    return out


def page_to_png(page, dpi: int = 300):
    """Rasterize a fitz.Page to PNG bytes for Vision, plus the pixel
    dimensions needed for map_bbox_to_page_rect()."""
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png"), pix.width, pix.height


def map_bbox_to_page_rect(nbbox, img_w: int, img_h: int, page_rect):
    """Convert a Vision normalized bottom-left bbox to a fitz.Rect in the
    page's point coordinate system (top-left origin)."""
    import fitz

    x, y, w, h = nbbox
    scale_x = page_rect.width / img_w
    scale_y = page_rect.height / img_h
    px0 = x * img_w
    px1 = (x + w) * img_w
    # Vision's y is measured from the BOTTOM; PyMuPDF's from the TOP —
    # flip, and note the box's "top" in page space is its bottom edge in
    # Vision's own coordinate frame (y + h from the bottom).
    py_top = img_h - (y + h) * img_h
    py_bottom = img_h - y * img_h
    return fitz.Rect(px0 * scale_x, py_top * scale_y,
                     px1 * scale_x, py_bottom * scale_y)
