#!/bin/bash
# Single-file wrapper: runs src/redact.py with the project's own Python
# environment so you don't need to activate anything.
#   ./scripts/redact.sh statement.pdf --preset financial --dry-run
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Setup has not been run yet. Easiest fix — run the batch script once:"
  echo "  $ROOT/scripts/run.sh"
  echo "or set up manually:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
exec "$PY" "$ROOT/src/redact.py" "$@"
