#!/usr/bin/env python3
"""
libreoffice_handler.py — Kind A convert handler using a local LibreOffice
install (docs/plans/expansion-plan.md §3.B). Covers .xls, .ppt, .odp (no
Tier-1 pure-Python reader exists for these) and, when office_converter is
set to "libreoffice", also .doc/.docx/.pptx/.odt as a fidelity option.

LibreOffice runs fully offline (`soffice --headless --convert-to pdf`).
Installation is consent-gated by scripts/run.sh — this module only uses
it if already present; it never installs anything itself.

Local only. No network. The one subprocess (soffice) is invoked as an
argument list — never through a shell — and given an isolated, per-call
temp profile directory so a batch run cannot corrupt or collide with the
user's real LibreOffice profile.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.exit(
        "PyMuPDF is not installed.\n"
        "Run:  pip install -r requirements.txt"
    )

try:  # imported as part of the handlers package (normal router path)
    from handlers.common import UnsupportedFormatError
except ImportError:  # executed directly: python src/handlers/libreoffice_handler.py
    from common import UnsupportedFormatError

SUPPORTED_EXTENSIONS = {".doc", ".docx", ".odt", ".xls", ".ppt", ".pptx", ".odp"}
_TIMEOUT_SECS = 120

_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "soffice",
)


def find_soffice() -> str | None:
    """Locate the soffice binary, or None if LibreOffice isn't installed."""
    for cand in _SOFFICE_CANDIDATES:
        if cand.startswith("/"):
            if Path(cand).exists():
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def to_pdf(input_path: Path, options: dict) -> tuple[bytes, dict]:
    """Convert via a headless LibreOffice instance. Returns (pdf_bytes, info)
    per the Kind A handler contract."""
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"libreoffice_handler cannot convert '{ext}' files — "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )
    soffice = find_soffice()
    if not soffice:
        raise UnsupportedFormatError(
            "LibreOffice is not installed. Install it with "
            "'brew install --cask libreoffice' (free, ~700 MB, fully "
            "offline), or export this file as .docx/.pptx/.xlsx first."
        )

    with tempfile.TemporaryDirectory(prefix="ai-redact-lo-") as tmp:
        tmp_dir = Path(tmp)
        # Isolated profile per call: a batch loop over many files must not
        # share/corrupt the user's real LibreOffice profile, and a stale
        # lock from a crashed prior run must never block this one.
        profile_dir = tmp_dir / "profile"
        outdir = tmp_dir / "out"
        outdir.mkdir()
        try:
            proc = subprocess.run(
                [
                    soffice, "--headless", "--norestore",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to", "pdf", "--outdir", str(outdir),
                    str(input_path),
                ],
                capture_output=True, text=True, timeout=_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired as exc:
            raise UnsupportedFormatError(
                f"LibreOffice timed out converting {input_path.name} "
                f"(>{_TIMEOUT_SECS}s) — the file may be corrupt, huge, or "
                "waiting on a blocked dialog."
            ) from exc

        produced = outdir / (input_path.stem + ".pdf")
        if proc.returncode != 0 or not produced.exists():
            detail = (proc.stderr or proc.stdout or "no output produced").strip()
            raise UnsupportedFormatError(
                f"LibreOffice could not convert {input_path.name} "
                f"({detail or 'unknown error'}). The file may be corrupt "
                "or password-protected."
            )
        pdf_bytes = produced.read_bytes()

    info = {
        "converter": "libreoffice",
        "dropped_elements": 0,  # LibreOffice renders the full layout; no
                                 # per-element drop accounting is available.
        "notes": [
            f"converted from {ext.lstrip('.')} via a local, offline "
            "LibreOffice instance (full layout fidelity, not simplified)."
        ],
    }
    return pdf_bytes, info
