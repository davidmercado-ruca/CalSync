#!/bin/bash
# Double-click so CalSync starts with your Mac and stays alive, with no Terminal
# windows open. Double-click again to update it or turn it off.
#
# WHY THIS SCRIPT COPIES THE CODE SOMEWHERE ELSE
#
# macOS protects ~/Documents, ~/Desktop and ~/Downloads with TCC. Your Terminal
# can read there because you allowed it once; a launchd service starts with no
# inherited permission and gets "Operation not permitted" on any file in those
# folders — including the Python interpreter itself.
#
# The alternative would be granting Full Disk Access to Python, which is a
# disproportionate permission: you would be granting it to ANY script run with
# that interpreter. Instead the service runs from ~/Library/Application
# Support/CalSync, which is not protected.
#
# This folder stays the source. Whenever the code changes, double-click here
# again and choose update.

# WHERE THE CODE COMES FROM
#
# This script works in two situations and tells them apart on its own:
#
#   - There is a `calsync` folder next to it → that is the code it installs.
#     This is David's copy: the source folder is the truth, so local changes
#     can be tested before they are published to anyone.
#   - There is not → it downloads the latest published version from GitHub.
#     This is a teammate's copy. They only ever need THIS file and their own
#     credentials.json, side by side in any folder.
#
# That is also how updates reach the team: a teammate double-clicks this file
# again and chooses 1, and they are on the newest version. No account, no
# password, no git.

cd "$(dirname "$0")" || exit 1
SOURCE="$(pwd -P)"
TARGET="$HOME/Library/Application Support/CalSync"
DATA="$HOME/.calsync"
LABEL="com.calsync.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Where the published code lives. Change REPO if the repository moves, and
# BRANCH to a tag (e.g. "v1.0") once more than a couple of people use this:
# with a tag, pushing a broken commit does not reach anybody until you move it.
REPO="davidmercado-ruca/CalSync"
BRANCH="main"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"

pause() { echo; read -r -p "  Press Enter to close."; exit "${1:-0}"; }

echo "──────────────────────────────────────────"
echo "  CalSync · start with my Mac"
echo "──────────────────────────────────────────"
echo

# --- Already installed: ask what to do ---------------------------------------
if [ -f "$PLIST" ]; then
  echo "  Autostart is ACTIVE."
  echo
  echo "    1) Update it to the newest version"
  echo "    2) Turn it off"
  echo "    3) Cancel"
  echo
  read -r -p "  Choose 1, 2 or 3: " choice
  case "$choice" in
    1)
      echo
      echo "  Updating…"
      # The running service has to go first, otherwise the port check below
      # trips on the very thing we are about to replace. `bootout` removes the
      # job, so KeepAlive does not bring it back while we work.
      echo "  Stopping the current service…"
      launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
        || launchctl unload "$PLIST" 2>/dev/null
      for _ in $(seq 1 15); do
        lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 || break
        sleep 1
      done
      ;;
    2)
      launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
        || launchctl unload "$PLIST" 2>/dev/null
      rm -f "$PLIST"
      echo
      echo "  ✓ Turned off. CalSync no longer starts on its own."
      echo "    Your data and Google permissions are untouched in $DATA"
      echo "    To run it by hand, double-click start.command."
      pause 0
      ;;
    *) echo; echo "  No changes."; pause 0 ;;
  esac
fi

# --- Checks ------------------------------------------------------------------
if [ ! -f credentials.json ]; then
  echo "  ⚠  credentials.json is missing. See STEP-1-Google-credentials.md"
  pause 1
fi

# By this point the service (if any) has been stopped, so anything still on the
# port is a copy someone started by hand.
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  ⚠  Something else is already using port 8765."
  echo "     It is most likely CalSync running in another Terminal window"
  echo "     (start.command). Close that window and try again."
  pause 1
fi

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "  Cannot find python3. Run this and try again:"
  echo "      xcode-select --install"
  pause 1
fi

# --- Put the code where launchd is allowed to read it -------------------------
mkdir -p "$TARGET" "$DATA" "$HOME/Library/LaunchAgents"

if [ -d "$SOURCE/calsync" ]; then
  echo "  Copying the code from this folder…"
  echo "    $TARGET"
  # Wiped first so a file you deleted here does not survive in the target.
  rm -rf "$TARGET/calsync"
  if ! cp -R "$SOURCE/calsync" "$TARGET/calsync" 2>/dev/null; then
    echo
    echo "  ✗ Could not read the code in this folder."
    echo "    If macOS showed a prompt asking for access to Documents, accept it"
    echo "    and try again. If you declined, enable it in:"
    echo "    System Settings → Privacy & Security → Files and Folders"
    pause 1
  fi
  if ! cp -f "$SOURCE/requirements.txt" "$TARGET/"; then
    echo "  ✗ Copy failed."
    pause 1
  fi
else
  echo "  Downloading the newest published version…"
  echo "    from github.com/$REPO ($BRANCH)"
  PREFIX="$(basename "$REPO")-$BRANCH"
  NUEVO="$TARGET/.descarga"
  rm -rf "$NUEVO"; mkdir -p "$NUEVO"

  # Download and unpack into a side folder, and only replace the working copy
  # once both steps worked. Otherwise a flaky wifi would leave the person with
  # no code at all — the old version is a far better outcome than none.
  if ! curl -fsSL -o "$NUEVO/code.tgz" "$TARBALL"; then
    rm -rf "$NUEVO"
    echo
    echo "  ✗ Could not download the code. Nothing was replaced."
    echo "    Check that you are online. If you are, the address in this script"
    echo "    may be wrong or the repository may be private:"
    echo "        https://github.com/$REPO"
    pause 1
  fi
  if ! tar -xzf "$NUEVO/code.tgz" -C "$NUEVO" --strip-components=1 \
         "$PREFIX/calsync" "$PREFIX/requirements.txt" 2>/dev/null; then
    rm -rf "$NUEVO"
    echo
    echo "  ✗ The download arrived damaged or with an unexpected shape."
    echo "    Nothing was replaced. Try again in a minute."
    pause 1
  fi

  rm -rf "$TARGET/calsync"
  mv "$NUEVO/calsync" "$TARGET/calsync"
  mv -f "$NUEVO/requirements.txt" "$TARGET/requirements.txt"
  rm -rf "$NUEVO"
fi

# Stale .pyc from the old copy can confuse Python if the code changed.
find "$TARGET/calsync" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp -f credentials.json "$TARGET/" || { echo "  ✗ Copy failed."; pause 1; }
chmod 600 "$TARGET/credentials.json"

# --- The service's own environment -------------------------------------------
if [ ! -x "$TARGET/.venv/bin/python" ]; then
  echo "  Installing the service dependencies (about a minute)…"
  "$PY" -m venv "$TARGET/.venv" || { echo "  ✗ Could not create the environment."; pause 1; }
fi
"$TARGET/.venv/bin/python" -m pip install --quiet --upgrade pip
if ! "$TARGET/.venv/bin/python" -m pip install --quiet -r "$TARGET/requirements.txt"; then
  echo "  ✗ Dependency install failed. Are you online?"
  pause 1
fi

if ! (cd "$TARGET" && ./.venv/bin/python -c "import calsync.app") 2>/tmp/calsync-import.txt; then
  echo "  ✗ The code does not load. The real error:"
  sed 's/^/     /' /tmp/calsync-import.txt
  pause 1
fi
echo "  ✓ Code and dependencies ready."
echo

# --- The service -------------------------------------------------------------
cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$TARGET/.venv/bin/python</string>
    <string>-m</string>
    <string>calsync</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$TARGET</string>
  <!-- Starts at login and comes back up on its own if it crashes. -->
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <!-- Background priority: it does not steal resources from your work. -->
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$DATA/out.log</string>
  <key>StandardErrorPath</key>
  <string>$DATA/errors.log</string>
</dict>
</plist>
PLIST_END

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if ! (launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null \
      || launchctl load -w "$PLIST" 2>/dev/null); then
  echo "  ✗ macOS refused the autostart. Check $DATA/errors.log"
  rm -f "$PLIST"
  pause 1
fi

# Noventa segundos y no treinta. Un arranque en frío —justo después de
# reemplazar los archivos— tiene que volver a cargar las librerías de Google
# sobre una base que ya pesa megas, y puede pasar del medio minuto. Con treinta
# el instalador se rendía mientras CalSync estaba arrancando perfectamente, y
# luego decía que había un error que no existía.
echo "  Service installed. Waiting for it to answer…"
for _ in $(seq 1 90); do
  if curl -s -o /dev/null --max-time 1 http://127.0.0.1:8765/; then
    echo
    echo "  ✓ CalSync is running in the background."
    echo
    echo "    Dashboard:  http://127.0.0.1:8765"
    echo "    It starts every time you log in to your Mac."
    echo
    if [ -d "$SOURCE/calsync" ]; then
      echo "    This folder stays the source of the code."
      echo "    Whenever it changes, double-click here again and choose 1."
    else
      echo "    To get the newest version later, double-click this file again"
      echo "    and choose 1. It downloads whatever is published at that moment."
    fi
    echo
    echo "    To turn it off, double-click here and choose 2."
    open http://127.0.0.1:8765
    pause 0
  fi
  sleep 1
done

echo
# Antes de dar nada por roto, preguntar si hay alguien escuchando. Que no haya
# contestado a tiempo y que no esté vivo son dos cosas muy distintas, y
# confundirlas manda a buscar un error que no existe.
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  ⚠  CalSync is up, but it took longer than 90 seconds to answer."
  echo "     That happens on a cold start right after an update. Nothing"
  echo "     is broken — give it a moment and use it as usual."
  echo
  echo "    Dashboard:  http://127.0.0.1:8765"
  echo "    It starts every time you log in to your Mac."
  echo
  open http://127.0.0.1:8765
  pause 0
fi

echo "  ✗ Nothing is listening on port 8765. The error is in:"
echo "    $DATA/errors.log"
echo
# Filtrado, no crudo. Las advertencias de Python 3.9 y de LibreSSL salen en
# cada arranque y nunca son el problema, y los avisos de ciclos que llegaron
# tarde son rutina. Sin quitarlos, las veinte líneas que se imprimen aquí son
# siempre ruido y el error de verdad queda fuera de la pantalla.
grep -v -e "FutureWarning" -e "NotOpenSSLWarning" -e "warnings.warn" \
        -e "was missed by" "$DATA/errors.log" 2>/dev/null \
  | tail -n 20 | sed 's/^/     /'
pause 1
