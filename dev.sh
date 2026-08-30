#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  RecoverAI -- macOS / Linux launcher.
#
#  Companion to dev.bat. Both are thin passthroughs to dev.py, which holds all
#  the actual logic exactly once.
#
#    ./dev.sh setup     ./dev.sh seed      ./dev.sh train
#    ./dev.sh start     ./dev.sh backend   ./dev.sh frontend
#    ./dev.sh test      ./dev.sh doctor    ./dev.sh demo
# ---------------------------------------------------------------------------
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer python3; fall back to python only if it is actually Python 3, since on
# some systems `python` still points at a Python 2 interpreter.
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)'; then
  PY=python
else
  echo "  [FAIL] Python 3 was not found on your PATH."
  echo "         Install Python 3.10+ from https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" "$HERE/dev.py" "$@"
