#!/bin/bash
# scripts/run.sh — one-command batch redaction.
#
# Sets up the local Python environment if needed (downloads dependencies),
# then redacts EVERY PDF in input/ and writes results to output/.
# The output/ folder is CLEANED at the start of each run.
#
# Usage (from anywhere):
#   ./scripts/run.sh                      # 'general' preset
#   ./scripts/run.sh financial            # choose a preset
#   ./scripts/run.sh medical --dry-run    # preview only (reports, no PDFs)
#
# Exit codes: 0 = all files OK, 1 = at least one file failed or needs OCR.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# --- arguments: optional preset first, everything else passed through -----
PRESET="general"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    PRESET="$1"
    shift
fi

# --- one-time setup: create venv and install dependencies -----------------
if [ ! -x .venv/bin/python ]; then
    echo "==> First run: creating Python environment (.venv)..."
    python3 -m venv .venv || { echo "ERROR: could not create venv"; exit 1; }
fi
if ! .venv/bin/python -c "import fitz, yaml, PIL, qrcode, zxingcpp" 2>/dev/null; then
    echo "==> Installing Python dependencies..."
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt || {
        echo "ERROR: dependency install failed"; exit 1; }
fi

# OCR support for scanned/image PDFs (Tesseract language data). Installed
# automatically when Homebrew is available; without it, scanned PDFs are
# skipped with a "Need OCR" notice instead of being redacted.
if ! command -v tesseract >/dev/null \
   && [ ! -d /opt/homebrew/share/tessdata ] \
   && [ ! -d /usr/local/share/tessdata ]; then
    if command -v brew >/dev/null; then
        echo "==> Installing Tesseract (OCR for scanned PDFs)..."
        brew install -q tesseract || \
            echo "    WARNING: OCR install failed — scanned PDFs will be skipped."
    else
        echo "NOTE: Homebrew not found; skipping OCR setup. Scanned PDFs"
        echo "      will be skipped. To enable OCR later: brew install tesseract"
    fi
fi
# Personal config: created from the committed template on first run. The
# real config is gitignored (it will contain your names/accounts).
if [ ! -f config/redact_config.yaml ]; then
    echo "==> Creating your personal config: config/redact_config.yaml"
    cp config/redact_config.example.yaml config/redact_config.yaml
    echo "    Add your names/terms to it — see the comments inside."
fi

# --- folders ---------------------------------------------------------------
mkdir -p input output

# Collect PDFs before cleaning output, so an empty input aborts harmlessly.
PDFS=()
for f in input/*.pdf input/*.PDF; do
    [ -e "$f" ] && PDFS+=("$f")
done
if [ ${#PDFS[@]} -eq 0 ]; then
    echo "No PDFs found in input/."
    echo "Drop the PDFs you want redacted into: $ROOT/input"
    echo "then run this script again."
    echo ""
    echo "(No documents handy? Generate fake test samples with:"
    echo "   .venv/bin/python src/make_sample_pdf.py )"
    exit 0
fi

echo "==> Cleaning output/ ..."
find output -mindepth 1 ! -name '.gitkeep' -delete

# --- process every PDF ------------------------------------------------------
echo "==> Redacting ${#PDFS[@]} PDF(s) with preset '$PRESET'"
OK=0
NEEDS_OCR=()
VERIFY_FAILED=()
FAILED=()
for f in "${PDFS[@]}"; do
    base="$(basename "$f")"
    stem="${base%.*}"
    echo ""
    echo "------------------------------------------------------------ $base"
    .venv/bin/python src/redact.py "$f" --preset "$PRESET" \
        -o "output/${stem}_redacted.pdf" "$@"
    rc=$?
    if [ $rc -eq 0 ]; then
        OK=$((OK + 1))
    elif [ $rc -eq 2 ]; then
        NEEDS_OCR+=("$base")   # unreadable page(s): redact.py already explained
    elif [ $rc -eq 3 ]; then
        VERIFY_FAILED+=("$base")
    else
        FAILED+=("$base")
    fi
done

# --- summary ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "BATCH SUMMARY"
echo "============================================================"
echo "  Succeeded : $OK of ${#PDFS[@]}"
if [ ${#NEEDS_OCR[@]} -gt 0 ]; then
    echo "  Unreadable: ${NEEDS_OCR[*]}"
    echo "              (page(s) could not be read even with OCR — those"
    echo "               pages are NOT redacted; see the file's report)"
fi
if [ ${#VERIFY_FAILED[@]} -gt 0 ]; then
    echo "  VERIFY FAILED: ${VERIFY_FAILED[*]}"
    echo "              *** sensitive text remains in these outputs —"
    echo "              *** DO NOT SHARE them; see their reports"
fi
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "  FAILED    : ${FAILED[*]}"
fi
echo "  Results   : $ROOT/output"
echo ""
echo "Check each *_report.txt for the verification result before sharing."

[ ${#NEEDS_OCR[@]} -eq 0 ] && [ ${#VERIFY_FAILED[@]} -eq 0 ] && [ ${#FAILED[@]} -eq 0 ] || exit 1
