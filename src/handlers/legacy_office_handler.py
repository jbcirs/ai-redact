#!/usr/bin/env python3
"""
legacy_office_handler.py — Kind A convert handler for the legacy Word
family: .doc, .odt, .rtf (docs/plans/expansion-plan.md §3.A).

Zero-install preprocessing step: macOS's built-in `textutil` binary
converts these formats to .docx, then the conversion is handed off to the
existing Tier-1 `office_handler._docx_to_pdf` converter, and the router
runs the proven PDF redaction/verification pipeline over the result.

textutil runs fully offline (it is a local macOS system binary; no
network access). The .docx it produces lives in the run's temp dir and
is deleted afterward. The original input file is never touched.

Local only. No network. Only one subprocess (textutil), invoked as an
argument list — never through a shell — so a filename cannot inject
extra shell syntax.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fitz  # noqa: F401  (imported for parity/early failure with other handlers)
except ImportError:  # pragma: no cover
    sys.exit(
        "PyMuPDF is not installed.\n"
        "Run:  pip install -r requirements.txt"
    )

try:  # imported as part of the handlers package (normal router path)
    from handlers.common import UnsupportedFormatError
    from handlers import office_handler
except ImportError:  # executed directly: python src/handlers/legacy_office_handler.py
    from common import UnsupportedFormatError
    import office_handler

SUPPORTED_EXTENSIONS = {".doc", ".odt", ".rtf"}

# textutil is a local, offline Apple system binary — not a network call.
_TEXTUTIL = "/usr/bin/textutil"
_TIMEOUT_SECS = 60


def to_pdf(input_path: Path, options: dict) -> tuple[bytes, dict]:
    """Convert a legacy Word-family file to simplified-PDF bytes.

    Chain: textutil -> temp .docx -> Tier-1 docx converter -> PDF bytes.
    Returns (pdf_bytes, info) per the Kind A handler contract.
    """
    input_path = Path(input_path)
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"legacy_office_handler cannot convert '{ext}' files — "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )

    with tempfile.TemporaryDirectory(prefix="ai-redact-legacy-") as tmp:
        tmp_docx = Path(tmp) / (input_path.stem + ".docx")
        _run_textutil(input_path, tmp_docx)
        pdf_bytes, info = office_handler._docx_to_pdf(tmp_docx, options or {})

    info["converter"] = f"textutil→{info.get('converter', 'tier1-python-docx')}"
    info.setdefault("notes", []).insert(
        0, f"converted from {ext.lstrip('.')} via macOS textutil, then Tier-1 docx"
    )
    return pdf_bytes, info


def _run_textutil(input_path: Path, output_docx: Path) -> None:
    if not Path(_TEXTUTIL).exists():
        raise UnsupportedFormatError(
            "textutil is not available (macOS-only tool) — cannot convert "
            f"'{input_path.suffix}' files on this system. Re-save the file "
            "as .docx manually and re-run."
        )
    try:
        proc = subprocess.run(
            [_TEXTUTIL, "-convert", "docx", "-output", str(output_docx),
             str(input_path)],
            capture_output=True, text=True, timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnsupportedFormatError(
            f"textutil timed out converting {input_path.name} "
            f"(>{_TIMEOUT_SECS}s) — the file may be corrupt or huge."
        ) from exc
    if proc.returncode != 0 or not output_docx.exists():
        detail = (proc.stderr or proc.stdout or "no output produced").strip()
        raise UnsupportedFormatError(
            f"textutil could not convert {input_path.name} "
            f"({detail or 'unknown error'}). The file may be corrupt, "
            "password-protected, or an unsupported legacy variant."
        )


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------


def _smoke_test() -> int:
    import tempfile as _tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from make_legacy_office_fixtures import PLANTED_EMAIL, make_rtf

    with _tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        rtf_path = make_rtf(tmp_dir)
        pdf_bytes, info = to_pdf(rtf_path, {})
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in pdf)
        pdf.close()
        assert PLANTED_EMAIL in text, "rtf: planted email missing from PDF"
        assert info["converter"].startswith("textutil→"), (
            f"unexpected converter id: {info['converter']}"
        )
        print(f"rtf info: {info}")

    print("legacy_office_handler smoke test: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_smoke_test())
    except AssertionError as exc:
        print(f"legacy_office_handler smoke test: FAIL — {exc}", file=sys.stderr)
        sys.exit(1)
