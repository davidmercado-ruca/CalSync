#!/bin/bash
# Double-click to run CalSync in this Terminal window.
# The first run installs what it needs into a .venv folder right here.

cd "$(dirname "$0")" || exit 1

pause() {
  echo
  echo "  ── Press Enter to close this window ──"
  read -r _
  exit "${1:-1}"
}

echo "──────────────────────────────────────────"
echo "  CalSync"
echo "──────────────────────────────────────────"
echo

# --- Find the best available Python ------------------------------------------
# macOS ships 3.9; if you have something newer installed we prefer it.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "  Cannot find python3."
  echo "  Run this in Terminal, accept the install, then try again:"
  echo
  echo "      xcode-select --install"
  pause 1
fi

echo "  Python: $($PY --version 2>&1)  ($(command -v $PY))"

# --- Environment -------------------------------------------------------------
if [ ! -d .venv ]; then
  echo "  First run: installing dependencies (about a minute)…"
  "$PY" -m venv .venv || { echo "  ✗ Could not create the environment."; pause 1; }
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  if ! ./.venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo "  ✗ Dependency install failed. Are you online?"
    pause 1
  fi
  echo "  ✓ Dependencies installed."
  echo
fi

# --- Check before starting ---------------------------------------------------
# If anything fails to import you see the real error HERE, not a blank window.
if ! ./.venv/bin/python -c "import calsync.app" 2>/tmp/calsync-import.txt; then
  echo "  ✗ The program could not load. The real error is:"
  echo
  sed 's/^/     /' /tmp/calsync-import.txt
  echo
  echo "  Copy and paste this text to whoever maintains CalSync."
  pause 1
fi

if [ ! -f credentials.json ]; then
  echo "  ⚠  credentials.json is missing in this folder."
  echo "     See STEP-1-Google-credentials.md"
  echo
fi

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  CalSync is already running."
  if [ -f "$HOME/Library/LaunchAgents/com.calsync.agent.plist" ]; then
    echo "  It starts with your Mac, so you do not need this window."
  else
    echo "  It must be in another Terminal window."
  fi
  echo
  echo "  Opening the dashboard…"
  open http://127.0.0.1:8765
  pause 0
fi

echo "  Dashboard:  http://127.0.0.1:8765"
echo "  To stop CalSync, close this window or press Ctrl-C."
echo

# Open the browser only once the server actually answers.
(
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null --max-time 1 http://127.0.0.1:8765/; then
      open http://127.0.0.1:8765
      exit 0
    fi
    sleep 0.5
  done
  echo "  ⚠  The server did not answer within 20 seconds. Open the link by hand."
) &

./.venv/bin/python -m calsync
code=$?

echo
if [ "$code" -ne 0 ] && [ "$code" -ne 130 ]; then
  echo "  ✗ CalSync stopped with an error (code $code)."
  echo "    Copy what is shown above and send it on."
  pause "$code"
fi
echo "  CalSync stopped."
