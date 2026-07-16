#!/bin/bash
# scripts/run.sh — one-command batch redaction.
#
# Sets up the local environment if needed (Python 3.13, dependencies, OCR),
# then redacts EVERY supported file in input/ and writes results to output/.
# Supported: PDF, images (jpg/png/tiff/heic/…), docx, pptx, xlsx, csv/tsv,
# and plain-text files. The output/ folder is CLEANED at the start of each
# run.
#
# Usage (from anywhere):
#   ./scripts/run.sh                      # 'general' preset
#   ./scripts/run.sh financial            # choose a preset
#   ./scripts/run.sh medical --dry-run    # preview only (reports, no files)
#   ./scripts/run.sh general --combine    # also write output/combined_redacted.pdf
#
# Exit codes: 0 = all files OK, 1 = at least one file failed/unsupported.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# --- arguments: optional preset first, everything else passed through -----
PRESET="general"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    PRESET="$1"
    shift
fi

# --combine isn't a redact.py flag — pull it out (and any --config value,
# needed so combine_outputs.py re-verifies with the exact same detector
# set) before building the args passed through per-file.
COMBINE=0
REDACT_ARGS=()
CONFIG_ARG=()
skip_next=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    a="${args[$i]}"
    if [ "$skip_next" -eq 1 ]; then
        CONFIG_ARG=(--config "$a")
        REDACT_ARGS+=("$a")
        skip_next=0
    elif [ "$a" = "--combine" ]; then
        COMBINE=1
    elif [ "$a" = "--config" ] || [ "$a" = "-c" ]; then
        REDACT_ARGS+=("$a")
        skip_next=1
    else
        REDACT_ARGS+=("$a")
    fi
done
if grep -qE '^\s*combine:\s*true\b' config/redact_config.yaml 2>/dev/null; then
    COMBINE=1
fi

# --- Python runtime: Homebrew 3.13 (system 3.9 is EOL and lacks wheels) ---
PYBIN=""
for cand in /opt/homebrew/opt/python@3.13/bin/python3.13 \
            /usr/local/opt/python@3.13/bin/python3.13; do
    [ -x "$cand" ] && PYBIN="$cand" && break
done
if [ -z "$PYBIN" ]; then
    if command -v brew >/dev/null; then
        echo "==> Installing Python 3.13 (one time)..."
        brew install -q python@3.13 || { echo "ERROR: Python install failed"; exit 1; }
        PYBIN="$(brew --prefix)/opt/python@3.13/bin/python3.13"
    else
        echo "ERROR: Homebrew not found. Install it from https://brew.sh,"
        echo "       then re-run this script."
        exit 1
    fi
fi

# Rebuild the venv whenever its Python doesn't match (e.g. after upgrade).
WANT="$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
HAVE="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ "$HAVE" != "$WANT" ]; then
    echo "==> Creating Python $WANT environment (.venv)..."
    rm -rf .venv
    "$PYBIN" -m venv .venv || { echo "ERROR: could not create venv"; exit 1; }
fi
if ! .venv/bin/python -c "import fitz, yaml, PIL, qrcode, zxingcpp, docx, pptx, openpyxl, pillow_heif, rawpy, extract_msg, msoffcrypto, Vision, Quartz" 2>/dev/null; then
    echo "==> Installing Python dependencies..."
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt || {
        echo "ERROR: dependency install failed"; exit 1; }
fi

# OCR support for scanned pages and images (Tesseract language data).
if ! command -v tesseract >/dev/null \
   && [ ! -d /opt/homebrew/share/tessdata ] \
   && [ ! -d /usr/local/share/tessdata ]; then
    if command -v brew >/dev/null; then
        echo "==> Installing Tesseract (OCR for scanned pages and images)..."
        brew install -q tesseract || \
            echo "    WARNING: OCR install failed — scanned pages will be skipped."
    else
        echo "NOTE: Homebrew not found; skipping OCR setup."
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

# Collect input files before cleaning output, so an empty input aborts
# harmlessly. Every regular file goes in; redact.py classifies by content
# and reports unsupported types honestly (exit code 4).
FILES=()
for f in input/*; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in .*) continue ;; esac
    FILES+=("$f")
done
if [ ${#FILES[@]} -eq 0 ]; then
    echo "No files found in input/."
    echo "Drop the documents you want redacted into: $ROOT/input"
    echo "then run this script again."
    echo ""
    echo "(No documents handy? Generate fake test samples with:"
    echo "   .venv/bin/python src/make_sample_pdf.py )"
    exit 0
fi

# --- LibreOffice consent gate (docs/plans/expansion-plan.md §3.B) ---------
# .xls/.ppt/.odp have no pure-Python reader; LibreOffice is the only way
# to convert them, and it's a ~700 MB install, so it is never installed
# silently — the user is asked, once, and the answer is cached.
NEED_LO=0
for f in "${FILES[@]}"; do
    case "$(basename "$f")" in
        *.[xX][lL][sS]|*.[pP][pP][tT]|*.[oO][dD][pP]) NEED_LO=1 ;;
    esac
done
if grep -qE '^[[:space:]]*office_converter:[[:space:]]*libreoffice' \
        config/redact_config.yaml 2>/dev/null; then
    NEED_LO=1
fi
LO_MARKER="config/.libreoffice_declined"
if [ "$NEED_LO" -eq 1 ] \
   && ! command -v soffice >/dev/null 2>&1 \
   && [ ! -x /Applications/LibreOffice.app/Contents/MacOS/soffice ]; then
    if [ -f "$LO_MARKER" ]; then
        echo "NOTE: .xls/.ppt/.odp file(s) present; LibreOffice was previously"
        echo "      declined — those files will be Unsupported this run."
        echo "      (delete $LO_MARKER to be asked again)"
    elif [ -t 0 ]; then
        echo ""
        echo "This batch includes .xls/.ppt/.odp file(s). Converting them needs"
        echo "LibreOffice — free, ~700 MB, and it runs fully offline (no network"
        echo "access; it's the same local-only guarantee as everything else here)."
        read -r -p "Install LibreOffice now via Homebrew? [y/N] " lo_ans
        case "$lo_ans" in
            [yY]*)
                brew install --cask libreoffice \
                    || echo "WARNING: LibreOffice install failed — those files "\
"will be Unsupported this run."
                ;;
            *)
                touch "$LO_MARKER"
                echo "Skipping — .xls/.ppt/.odp file(s) will be Unsupported this run."
                ;;
        esac
    else
        echo "NOTE: .xls/.ppt/.odp file(s) present but no interactive terminal"
        echo "      for the LibreOffice install prompt — skipping (Unsupported"
        echo "      this run). Run interactively once to install, or:"
        echo "      brew install --cask libreoffice"
    fi
fi

echo "==> Cleaning output/ ..."
find output -mindepth 1 ! -name '.gitkeep' -delete
mkdir -p output/logs

# --- process every file -----------------------------------------------------
echo "==> Redacting ${#FILES[@]} file(s) with preset '$PRESET'"
OK=0
NEEDS_OCR=()
VERIFY_FAILED=()
UNSUPPORTED=()
ENCRYPTED=()
FAILED=()
for f in "${FILES[@]}"; do
    base="$(basename "$f")"
    echo ""
    echo "------------------------------------------------------------ $base"
    .venv/bin/python src/redact.py "$f" --preset "$PRESET" -o output/ "${REDACT_ARGS[@]}"
    rc=$?
    if [ $rc -eq 0 ]; then
        OK=$((OK + 1))
    elif [ $rc -eq 2 ]; then
        NEEDS_OCR+=("$base")   # unreadable page(s): redact.py already explained
    elif [ $rc -eq 3 ]; then
        VERIFY_FAILED+=("$base")
    elif [ $rc -eq 4 ]; then
        UNSUPPORTED+=("$base")
    elif [ $rc -eq 5 ]; then
        ENCRYPTED+=("$base")   # password-protected, couldn't open: redact.py already explained
    else
        FAILED+=("$base")
    fi
done

# --- combine (docs/plans/expansion-plan.md §3.H) ---------------------------
# An EXTRA artifact alongside the individual outputs, never instead of them.
if [ "$COMBINE" -eq 1 ]; then
    if [ "$OK" -gt 0 ]; then
        echo ""
        echo "==> Combining redacted outputs into one PDF..."
        .venv/bin/python src/combine_outputs.py output/ --preset "$PRESET" "${CONFIG_ARG[@]}"
        [ $? -ne 0 ] && VERIFY_FAILED+=("combined_redacted.pdf")
    else
        echo ""
        echo "Skipping --combine: no successfully redacted files to merge."
    fi
fi

# --- summary ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "BATCH SUMMARY"
echo "============================================================"
echo "  Succeeded : $OK of ${#FILES[@]}"
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
if [ ${#UNSUPPORTED[@]} -gt 0 ]; then
    echo "  Unsupported: ${UNSUPPORTED[*]}"
    echo "              (file type not handled — export to PDF/docx/xlsx/"
    echo "               image/text and re-run)"
fi
if [ ${#ENCRYPTED[@]} -gt 0 ]; then
    echo "  Encrypted : ${ENCRYPTED[*]}"
    echo "              (password-protected, could not open — use --password"
    echo "               or add it to the config's passwords: map)"
fi
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "  FAILED    : ${FAILED[*]}"
fi
echo "  Results   : $ROOT/output"
echo "  Reports   : $ROOT/output/logs"
echo ""
echo "Check each report in output/logs for the verification result before sharing."

[ ${#NEEDS_OCR[@]} -eq 0 ] && [ ${#VERIFY_FAILED[@]} -eq 0 ] \
    && [ ${#UNSUPPORTED[@]} -eq 0 ] && [ ${#ENCRYPTED[@]} -eq 0 ] \
    && [ ${#FAILED[@]} -eq 0 ] || exit 1
