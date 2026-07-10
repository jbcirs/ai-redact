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
echo "==> Checking outputs for planted PII"
"$PY" tests/check_outputs.py "$OUT" || FAILED=1

if [ $FAILED -ne 0 ]; then
    echo ""
    echo "TEST SUITE FAILED"
    exit 1
fi
echo ""
echo "TEST SUITE PASSED"
