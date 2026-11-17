#!/bin/bash
# OpenJarvis + Zero update — fast, low-storage, keeps Zero as the engine.
#
#   git pull  ->  editable install (code is already linked, deps only)  ->  restore
#   Zero engine + jarvis agent + config  ->  restart  ->  verify
#
# Nothing here installs Rust, Ollama, llama.cpp, or a model. The engine is Zero.
set -uo pipefail

SRC="$HOME/openjarvis"
VENV="$HOME/.openjarvis-venv/bin"
VENV_PY="$HOME/.openjarvis-venv/bin/python"
SITE="$HOME/.openjarvis-venv/lib/python3.13/site-packages"
CANON="/usr/local/share/zero-jarvis/canonical"

say() { echo ">>> $*"; }

say "updating OpenJarvis source (git fetch + ff-only merge)"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch origin && git -C "$SRC" merge --ff-only origin/main \
    || say "git update skipped (local changes or diverged)"
else
  say "no git checkout at $SRC — skipping source update"
fi

say "restoring any files that went missing"
bash "$CANON/restore.sh" --quiet || true

say "refreshing the editable install (deps only, no cache)"
cd "$SRC" || exit 1
"$VENV/pip" install -e . --no-deps --no-cache-dir -q 2>/tmp/opencode/openjarvis-pip.log \
  || { tail -20 /tmp/opencode/openjarvis-pip.log; }

say "re-applying the Zero engine + jarvis agent into the package"
bash "$CANON/install-customisations.sh"

say "restarting services"
systemctl --user restart zero-engine.service 2>/dev/null || true
pkill -f "jarvis serve" 2>/dev/null || true

say "verifying"
sleep 1
curl -sf http://127.0.0.1:8642/health >/dev/null \
  && echo "    zero engine: OK ($(curl -s http://127.0.0.1:8642/health | head -c 120)...)" \
  || echo "    zero engine: NOT RESPONDING"
"$VENV/jarvis" doctor >/tmp/opencode/openjarvis-doctor.log 2>&1 || true
echo "    doctor log: /tmp/opencode/openjarvis-doctor.log"

say "update complete"
