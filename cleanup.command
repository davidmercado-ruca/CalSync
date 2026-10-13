#!/bin/bash
# Double-click to delete EVERYTHING CalSync created in your calendars.
# Your own events cannot be touched.

cd "$(dirname "$0")" || exit 1

echo
if [ ! -x ./.venv/bin/python ]; then
  echo "  Environment missing. Run start.command once first."
  echo
  read -r -p "  Press Enter to close."
  exit 1
fi

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  ⚠  CalSync is running. Close it first (the other Terminal window, or"
  echo "     turn off autostart.command) so it does not recreate what we delete."
  echo
  read -r -p "  Press Enter to close."
  exit 1
fi

./.venv/bin/python -m calsync.purge
code=$?

echo
read -r -p "  Press Enter to close."
exit $code
