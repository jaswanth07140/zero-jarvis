#!/bin/bash
# Self-healing guard for the Zero/Jarvis install.
#
# Files under $HOME have been disappearing minutes after they are written on
# this machine. Everything that matters is therefore mirrored in
# /usr/local/share/zero-jarvis/canonical and put back here if it goes missing.
# Only *missing* files are restored — newer local edits are never overwritten.
#
# Runs every minute from zero-guard.timer (system unit).
set -uo pipefail

CANON="/usr/local/share/zero-jarvis/canonical"
LOGDIR="/usr/local/share/zero-jarvis"
LOG="$LOGDIR/restore.log"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

log() { echo "$(date -Is) $*" >> "$LOG"; }
note() { [ "$QUIET" = 1 ] || echo "$*"; }

changed=0
mkdir -p "$LOGDIR" 2>/dev/null || true

# --- 1. zero-ai brain ------------------------------------------------------
if [ ! -f "$HOME/zero-ai/runtime/brain.py" ] \
   || [ ! -f "$HOME/zero-ai/runtime/zero_engine.py" ] \
   || [ ! -f "$HOME/zero-ai/identity/rules.md" ] \
   || [ ! -f "$HOME/zero-ai/identity/persona.md" ]; then
  mkdir -p "$HOME/zero-ai"
  if [ -f "$CANON/zero-ai.tar.gz" ]; then
    tar -xzf "$CANON/zero-ai.tar.gz" -C "$HOME/zero-ai" --no-same-owner 2>/dev/null
    chmod +x "$HOME/zero-ai/runtime/zero_engine.py" 2>/dev/null
    log "restored zero-ai brain from canonical snapshot"
    note "restored zero-ai brain"
    changed=1
  else
    log "ERROR: $CANON/zero-ai.tar.gz missing"
  fi
fi

# --- 2. knowledge files (any removed domain file) -------------------------
missing_knowledge=$(find "$HOME/zero-ai/knowledge" -name "*.md" 2>/dev/null | wc -l)
if [ "$missing_knowledge" -lt 40 ] && [ -f "$CANON/zero-ai.tar.gz" ]; then
  tar -xzf "$CANON/zero-ai.tar.gz" -C "$HOME/zero-ai" --no-same-owner 2>/dev/null
  log "restored knowledge files (found only $missing_knowledge)"
  note "restored knowledge files"
  changed=1
fi

# --- 3. update + repair scripts in ~/zero-ops -----------------------------
mkdir -p "$HOME/zero-ops"
for f in openjarvis-update.sh openjarvis-jarvis-agent.py; do
  if [ ! -f "$HOME/zero-ops/$f" ]; then
    cp "$CANON/$f" "$HOME/zero-ops/$f" 2>/dev/null && chmod +x "$HOME/zero-ops/$f"
    log "restored zero-ops/$f"
    note "restored zero-ops/$f"
    changed=1
  fi
done

# --- 4. OpenJarvis customisations ----------------------------------------
if [ ! -f "$HOME/openjarvis/src/openjarvis/engine/zero_brain.py" ] \
   || ! grep -q zero_brain "$HOME/openjarvis/src/openjarvis/engine/__init__.py" 2>/dev/null \
   || ! grep -q "zero: only expose" "$HOME/openjarvis/src/openjarvis/server/routes.py" 2>/dev/null \
   || [ ! -f "$HOME/.openjarvis/inference.json" ] \
   || ! grep -q 'default = "zero"' "$HOME/.openjarvis/config.toml" 2>/dev/null; then
  bash "$CANON/install-customisations.sh" >>"$LOG" 2>&1 \
    && { log "re-applied OpenJarvis customisations"; note "re-applied OpenJarvis customisations"; changed=1; }
fi

# --- 4b. uv shim for the desktop app ------------------------------------
if [ ! -f "$HOME/.local/bin/uv" ] || ! grep -q "uv shim" "$HOME/.local/bin/uv" 2>/dev/null; then
  mkdir -p "$HOME/.local/bin"
  if [ -f "$HOME/.local/bin/uv" ] && [ ! -f "$HOME/.local/bin/uv.real" ]; then
    cp -a "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.real"
  fi
  cp "$CANON/uv" "$HOME/.local/bin/uv" 2>/dev/null && chmod +x "$HOME/.local/bin/uv"
  cp "$CANON/uv" "$HOME/zero-ops/uv" 2>/dev/null
  log "restored uv shim"
  changed=1
fi

# --- 4c. project-root symlink the desktop app looks for ------------------
# A stock OpenJarvis install drops a real clone at ~/OpenJarvis and replaces
# our link. Move that clone aside and point the path back at the Zero tree.
if [ -f "$HOME/openjarvis/pyproject.toml" ]; then
  if [ -e "$HOME/OpenJarvis" ] && [ ! -L "$HOME/OpenJarvis" ]; then
    rm -rf "$HOME/.openjarvis-stock-backup" 2>/dev/null
    mv "$HOME/OpenJarvis" "$HOME/.openjarvis-stock-backup" 2>/dev/null \
      && log "moved stock ~/OpenJarvis clone to ~/.openjarvis-stock-backup"
    changed=1
  fi
  if [ ! -L "$HOME/OpenJarvis" ] || [ "$(readlink -f "$HOME/OpenJarvis" 2>/dev/null)" != "$(readlink -f "$HOME/openjarvis" 2>/dev/null)" ]; then
    ln -sfn "$HOME/openjarvis" "$HOME/OpenJarvis" 2>/dev/null
    log "restored ~/OpenJarvis -> ~/openjarvis"
    changed=1
  fi
fi

# --- 5. systemd unit ------------------------------------------------------
if [ ! -f "$HOME/.config/systemd/user/zero-engine.service" ]; then
  mkdir -p "$HOME/.config/systemd/user"
  cp "$CANON/zero-engine.service" "$HOME/.config/systemd/user/" 2>/dev/null
  systemctl --user daemon-reload 2>/dev/null
  systemctl --user enable zero-engine.service 2>/dev/null
  log "restored zero-engine.service unit"
  changed=1
fi

# --- 6. keep the engine running ------------------------------------------
if ! curl -sf --max-time 3 http://127.0.0.1:8642/health >/dev/null 2>&1; then
  systemctl --user start zero-engine.service 2>/dev/null || true
  sleep 1
  if curl -sf --max-time 3 http://127.0.0.1:8642/health >/dev/null 2>&1; then
    log "zero-engine was down — started it"
    note "zero-engine restarted"
    changed=1
  else
    log "WARNING: zero-engine still not responding"
  fi
fi

# --- 7. nothing to do -----------------------------------------------------
if [ "$changed" = 0 ] && [ "$QUIET" = 0 ]; then
  echo "all Zero/Jarvis files present, engine healthy"
fi

# keep the log small
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 200000 ]; then
  tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
