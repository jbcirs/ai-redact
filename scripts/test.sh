#!/bin/bash
# scripts/test.sh — planted-PII regression suite for every supported format.
#
# Generates fake fixtures (each planting known identifiers + a financial
# string that must SURVIVE), redacts them all, then asserts the planted
# values are gone from every output and the financial string remains.
# Exits non-zero on any failure. Runs entirely locally in a temp dir —
# never touches input/ or output/.
#
# Usage:  ./scripts/test.sh

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
PY=.venv/bin/python

[ -x "$PY" ] || { echo "Run ./scripts/run.sh once first (creates .venv)"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
IN="$WORK/in"; OUT="$WORK/out"
mkdir -p "$IN" "$OUT"

echo "==> Generating fixtures in $IN"
"$PY" tests/make_text_fixtures.py "$IN" || exit 1
"$PY" tests/make_image_fixtures.py "$IN" || exit 1
"$PY" tests/make_office_fixtures.py "$IN" || exit 1
"$PY" tests/make_legacy_office_fixtures.py "$IN" || exit 1
"$PY" tests/make_email_fixtures.py "$IN" || exit 1
"$PY" tests/make_epub_fixtures.py "$IN" || exit 1
# One PDF fixture with the same planted values.
"$PY" - "$IN" <<'EOF' || exit 1
import sys, fitz
page_text = """Statement of Casey Plantedname
Email: planted.email@example.com   Phone: (555) 010-9999
SSN: 000-55-4444
Balance due: $12,345.67 (must survive)"""
doc = fitz.open(); page = doc.new_page()
page.insert_text((50, 80), page_text, fontname="courier", fontsize=11, lineheight=1.5)
doc.save(sys.argv[1] + "/fixture.pdf"); doc.close()
print("wrote fixture.pdf")
EOF

# Test config: the planted name as a custom term; all switches default.
CFG="$WORK/test_config.yaml"
cat > "$CFG" <<'EOF'
preset: general
custom_terms:
  names:
    - "Casey Plantedname"
EOF

echo ""
echo "==> Redacting every fixture"
FAILED=0
for f in "$IN"/*; do
    base="$(basename "$f")"
    "$PY" src/redact.py "$f" --config "$CFG" -o "$OUT/" \
        > "$WORK/log_$base.txt" 2>&1
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "  ✗ $base exited rc=$rc"
        tail -5 "$WORK/log_$base.txt" | sed 's/^/      /'
        FAILED=1
    else
        echo "  ✓ $base"
    fi
done

echo ""
echo "==> everything:pdf (options.output.everything) on a native fixture"
CFG_PDF="$WORK/test_config_everything_pdf.yaml"
cat > "$CFG_PDF" <<'EOF'
preset: general
custom_terms:
  names:
    - "Casey Plantedname"
options:
  output:
    everything: pdf
EOF
# A fresh copy under its own name — reprocessing fixture.csv itself would
# recompute the SAME deterministic output path as the main loop already
# used and silently overwrite/delete that artifact (output naming depends
# only on input path + output format, not which config produced it).
cp "$IN/fixture.csv" "$IN/everything_test.csv"
"$PY" src/redact.py "$IN/everything_test.csv" --config "$CFG_PDF" -o "$OUT/" \
    > "$WORK/log_everything_pdf.txt" 2>&1
rc=$?
if [ $rc -ne 0 ] || [ ! -f "$OUT/everything_test_csv_redacted.pdf" ]; then
    echo "  ✗ everything:pdf did not produce everything_test_csv_redacted.pdf (rc=$rc)"
    tail -5 "$WORK/log_everything_pdf.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ everything_test_csv_redacted.pdf (forced PDF output)"
fi

echo ""
echo "==> encrypted files (--password / config passwords: map, exit code 5)"
# Generated into their own dir, not $IN: without a password these correctly
# exit 5, which would otherwise trip the main loop's generic "rc != 0 is a
# failure" check above.
ENC="$WORK/encrypted"
mkdir -p "$ENC"
"$PY" tests/make_encrypted_fixtures.py "$ENC" || exit 1
TEST_PASSWORD="TestPass123!"   # must match tests/make_encrypted_fixtures.py

"$PY" src/redact.py "$ENC/protected.pdf" --config "$CFG" -o "$OUT/" \
    > "$WORK/log_enc_nopass.txt" 2>&1
rc=$?
if [ $rc -ne 5 ]; then
    echo "  ✗ protected.pdf without a password should exit 5, got rc=$rc"
    tail -5 "$WORK/log_enc_nopass.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ protected.pdf correctly refused without a password (rc=5)"
fi

for name in protected.pdf protected.xlsx; do
    "$PY" src/redact.py "$ENC/$name" --config "$CFG" --password "$TEST_PASSWORD" \
        -o "$OUT/" > "$WORK/log_enc_$name.txt" 2>&1
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "  ✗ $name with the correct password exited rc=$rc"
        tail -5 "$WORK/log_enc_$name.txt" | sed 's/^/      /'
        FAILED=1
    else
        echo "  ✓ $name (correct password)"
    fi
done

echo ""
echo "==> Apple Vision (handwriting_ocr / redact_handwriting / redact_faces)"
# Generated into its own dir: needs the vision options enabled, which are
# NOT in the main loop's $CFG (they default off), so a run against the
# shared config wouldn't redact the planted name and would falsely fail
# the check_outputs.py sweep below.
VIS="$WORK/vision"
mkdir -p "$VIS"
"$PY" tests/make_vision_fixtures.py "$VIS" || exit 1

CFG_HANDWRITING_OCR="$WORK/cfg_handwriting_ocr.yaml"
cat > "$CFG_HANDWRITING_OCR" <<'EOF'
preset: general
custom_terms:
  names:
    - "Casey Plantedname"
options:
  handwriting_ocr: true
EOF
cp "$VIS/handwriting.png" "$VIS/handwriting_ocr_test.png"
"$PY" src/redact.py "$VIS/handwriting_ocr_test.png" --config "$CFG_HANDWRITING_OCR" \
    -o "$OUT/" > "$WORK/log_vision_ocr.txt" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo "  ✗ handwriting_ocr run exited rc=$rc"
    tail -8 "$WORK/log_vision_ocr.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ handwriting_ocr_test_redacted.png (targeted match via Vision OCR)"
fi

CFG_HANDWRITING_BLANKET="$WORK/cfg_handwriting_blanket.yaml"
cat > "$CFG_HANDWRITING_BLANKET" <<'EOF'
preset: general
options:
  redact_handwriting: true
EOF
cp "$VIS/handwriting.png" "$VIS/handwriting_blanket_test.png"
"$PY" src/redact.py "$VIS/handwriting_blanket_test.png" --config "$CFG_HANDWRITING_BLANKET" \
    -o "$OUT/" > "$WORK/log_vision_blanket.txt" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo "  ✗ redact_handwriting run exited rc=$rc"
    tail -8 "$WORK/log_vision_blanket.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ handwriting_blanket_test_redacted.png (blanket handwriting redaction)"
fi

# redact_faces: integration-only (no crash, pipeline completes) — there is
# no synthetic image that reliably triggers Vision's face detector and no
# real photo available offline; see tests/make_vision_fixtures.py docstring.
CFG_FACES="$WORK/cfg_faces.yaml"
cat > "$CFG_FACES" <<'EOF'
preset: general
options:
  redact_faces: true
EOF
cp "$VIS/handwriting.png" "$VIS/faces_test.png"
"$PY" src/redact.py "$VIS/faces_test.png" --config "$CFG_FACES" \
    -o "$OUT/" > "$WORK/log_vision_faces.txt" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo "  ✗ redact_faces run exited rc=$rc"
    tail -8 "$WORK/log_vision_faces.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ faces_test_redacted.png (redact_faces integration, no crash)"
fi

echo ""
echo "==> Checking outputs for planted PII"
"$PY" tests/check_outputs.py "$OUT" || FAILED=1

echo ""
echo "==> combine (options.output.combine / run.sh --combine)"
"$PY" src/combine_outputs.py "$OUT" --config "$CFG" > "$WORK/log_combine.txt" 2>&1
rc=$?
if [ $rc -ne 0 ] || [ ! -f "$OUT/combined_redacted.pdf" ]; then
    echo "  ✗ combine_outputs.py failed (rc=$rc)"
    tail -10 "$WORK/log_combine.txt" | sed 's/^/      /'
    FAILED=1
else
    echo "  ✓ combined_redacted.pdf"
    # The combined PDF must ALSO be clean — re-run the same PII check
    # against just it, isolated from the rest of $OUT.
    COMBINE_CHECK="$WORK/combine_check"
    mkdir -p "$COMBINE_CHECK"
    cp "$OUT/combined_redacted.pdf" "$COMBINE_CHECK/"
    "$PY" tests/check_outputs.py "$COMBINE_CHECK" || FAILED=1
fi

if [ $FAILED -ne 0 ]; then
    echo ""
    echo "TEST SUITE FAILED"
    exit 1
fi
echo ""
echo "TEST SUITE PASSED"
