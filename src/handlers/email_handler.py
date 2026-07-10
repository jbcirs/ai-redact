#!/usr/bin/env python3
"""
email_handler.py — Kind A convert handler + attachment extraction for
.eml / .msg (docs/plans/expansion-plan.md §3.E).

to_pdf() renders headers (From/To/Cc/Subject/Date — PII-dense, scanned
like any text) and body through the shared pdf_render flow, then rides
the normal PDF redaction/verification pipeline. Attachments are handled
separately by extract_attachments() — the router's run_email_flow()
recurses each one through the format router and redacts it independently
(its own output, own report), capped at 50 attachments / 100 MB each
(docs/plans/expansion-plan.md §6 grill item 3).

Local only. No network. .eml is parsed with the stdlib email module;
.msg needs the extract-msg package (pure Python).
"""

from __future__ import annotations

import email
import sys
from email import policy
from pathlib import Path

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
except ImportError:  # executed directly: python src/handlers/email_handler.py
    from common import UnsupportedFormatError
    from pdf_render import PdfFlow, html_to_text

SUPPORTED_EXTENSIONS = {".eml", ".msg"}
MAX_ATTACHMENTS = 50
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB
_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date")


# ---------------------------------------------------------------------------
# .eml (stdlib email module)
# ---------------------------------------------------------------------------


def _parse_eml(input_path: Path):
    with open(input_path, "rb") as f:
        return email.message_from_binary_file(f, policy=policy.default)


def _eml_headers_text(msg) -> str:
    lines = [f"{field}: {msg.get(field)}" for field in _HEADER_FIELDS if msg.get(field)]
    return "\n".join(lines)


def _eml_body_text(msg) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        content = html_to_text(content)
    return content


def _eml_attachments(msg) -> list:
    out = []
    try:
        parts = list(msg.iter_attachments())
    except Exception:
        return out
    for part in parts:
        name = part.get_filename() or "attachment"
        try:
            blob = part.get_content()
            if isinstance(blob, str):
                blob = blob.encode("utf-8", "replace")
        except Exception:
            out.append((name, b"", "could not decode attachment content"))
            continue
        out.append((name, blob, None))
    return out


# ---------------------------------------------------------------------------
# .msg (extract-msg)
# ---------------------------------------------------------------------------


def _open_msg(input_path: Path):
    try:
        import extract_msg
    except ImportError as exc:
        raise UnsupportedFormatError(
            "extract-msg is not installed — run "
            "'pip install -r requirements.txt' to read .msg files."
        ) from exc
    return extract_msg.Message(str(input_path))


def _msg_headers_text(msg) -> str:
    fields = (("From", getattr(msg, "sender", None)),
              ("To", getattr(msg, "to", None)),
              ("Cc", getattr(msg, "cc", None)),
              ("Subject", getattr(msg, "subject", None)),
              ("Date", getattr(msg, "date", None)))
    return "\n".join(f"{label}: {val}" for label, val in fields if val)


def _msg_attachments(msg) -> list:
    out = []
    for att in getattr(msg, "attachments", []) or []:
        name = (getattr(att, "longFilename", None)
                or getattr(att, "shortFilename", None) or "attachment")
        try:
            blob = att.data
            if not isinstance(blob, bytes):
                out.append((name, b"", "unreadable attachment data"))
                continue
        except Exception:
            out.append((name, b"", "could not read attachment data"))
            continue
        out.append((name, blob, None))
    return out


# ---------------------------------------------------------------------------
# Handler entry points
# ---------------------------------------------------------------------------


def to_pdf(input_path: Path, options: dict) -> tuple[bytes, dict]:
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext == ".eml":
        msg = _parse_eml(input_path)
        header_text = _eml_headers_text(msg)
        body_text = _eml_body_text(msg)
    elif ext == ".msg":
        msg = _open_msg(input_path)
        header_text = _msg_headers_text(msg)
        body_text = msg.body or ""
    else:
        raise UnsupportedFormatError(
            f"email_handler cannot convert '{ext}' files — supported: "
            ".eml, .msg."
        )

    flow = PdfFlow()
    flow.add_text(header_text or "(no headers found)", size=10.0)
    flow.add_gap(10)
    flow.add_text(body_text.strip() or "(no readable body)", size=11.0)
    info = {
        "converter": "tier1-email",
        "dropped_elements": 0,
        "notes": [
            "headers (From/To/Cc/Subject/Date) and body rendered to PDF; "
            "attachments (if any) are redacted separately as their own "
            "output files"
        ],
    }
    return flow.to_bytes(), info


def extract_attachments(input_path: Path) -> list:
    """Returns [(filename, bytes, skip_reason_or_None), ...], capped at
    MAX_ATTACHMENTS count / MAX_ATTACHMENT_BYTES each. Never raises for a
    missing/unreadable attachment — that becomes a skip_reason instead."""
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext == ".eml":
        raw = _eml_attachments(_parse_eml(input_path))
    elif ext == ".msg":
        try:
            raw = _msg_attachments(_open_msg(input_path))
        except UnsupportedFormatError:
            return []
    else:
        return []

    out = []
    for i, (name, blob, reason) in enumerate(raw, 1):
        if i > MAX_ATTACHMENTS:
            out.append((name, blob,
                        f"skipped: more than {MAX_ATTACHMENTS} attachments"))
        elif reason:
            out.append((name, blob, reason))
        elif len(blob) > MAX_ATTACHMENT_BYTES:
            out.append((name, blob,
                        f"skipped: exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)} "
                        f"MB cap"))
        else:
            out.append((name, blob, None))
    return out
