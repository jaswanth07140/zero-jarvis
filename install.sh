#!/usr/bin/env bash
# Zero-Jarvis installer / repair.
#
# Restores Zero as the engine for the OpenJarvis desktop app:
#   * the Zero brain (~/zero-ai) — no Ollama, no llama.cpp, no weights
#   * the `zero` engine + `jarvis` agent inside the OpenJarvis checkout
#   * ~/.openjarvis config pointing at http://127.0.0.1:8642
#   * a uv shim (no Rust toolchain needed)
#   * the zero-engine service + a minute-ly self-healing guard
#
# Safe to run repeatedly — after any stock OpenJarvis install, run this and
# Zero comes back.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAN=/usr/local/share/zero-jarvis/canonical
VENV="$HOME/.openjarvis-venv"

say() { printf '\033[1;36m>>> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 1. payload
say "installing canonical payload to $CAN"
sudo mkdir -p "$CAN"
sudo chown -R "$(id -u):$(id -g)" /usr/local/share/zero-jarvis
cp -a "$HERE/files/." "$CAN/"
chmod +x "$CAN"/*.sh "$CAN/uv" 2>/dev/null || true

# ---------------------------------------------------------------- 2. checkout
say "locating the OpenJarvis checkout"
ROOT=""
if [ -f "$HOME/openjarvis/pyproject.toml" ]; then
  ROOT="$HOME/openjarvis"
elif [ -f "$HOME/OpenJarvis/pyproject.toml" ] && [ ! -L "$HOME/OpenJarvis" ]; then
  ROOT="$HOME/OpenJarvis"
elif [ -d "$HOME/openjarvis/.git" ]; then
  git -C "$HOME/openjarvis" restore . || true
  [ -f "$HOME/openjarvis/pyproject.toml" ] && ROOT="$HOME/openjarvis"
fi

if [ -z "$ROOT" ]; then
  echo "No OpenJarvis checkout found (looked for ~/openjarvis and ~/OpenJarvis)."
  echo "Clone it once, then re-run this script:"
  echo "  git clone --depth 1 https://github.com/open-jarvis/OpenJarvis.git ~/openjarvis"
  exit 1
fi
say "checkout: $ROOT"

# The desktop app only scans capital-O paths — point ~/OpenJarvis at our tree.
if [ ! -e "$HOME/OpenJarvis/pyproject.toml" ]; then
  rm -rf "$HOME/OpenJarvis" 2>/dev/null || true
  ln -sfn "$ROOT" "$HOME/OpenJarvis"
  say "linked ~/OpenJarvis -> $ROOT"
fi

# ---------------------------------------------------------------- 3. venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "Python venv missing at $VENV."
  echo "Create it once:  python3 -m venv $VENV && $VENV/bin/pip install -e $ROOT --no-deps"
  exit 1
fi

# ---------------------------------------------------------------- 4. restore
say "restoring brain, engine, agent, config and uv shim"
bash "$CAN/restore.sh"

# ---------------------------------------------------------------- 5. service
say "restoring services"
mkdir -p "$HOME/.config/systemd/user"
cp "$CAN/zero-engine.service" "$HOME/.config/systemd/user/" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable --now zero-engine.service 2>/dev/null || true
systemctl --user restart zero-engine.service 2>/dev/null || true

say "installing the self-healing guard (every minute)"
sudo cp "$CAN/zero-guard.service" /etc/systemd/system/ 2>/dev/null || true
sudo cp "$CAN/zero-guard.timer" /etc/systemd/system/ 2>/dev/null || true
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl enable --now zero-guard.timer 2>/dev/null || true

# ---------------------------------------------------------------- 6. verify
sleep 2
if curl -sf --max-time 5 http://127.0.0.1:8642/health >/dev/null; then
  say "zero engine: $(curl -s http://127.0.0.1:8642/health | head -c 140)"
else
  echo "WARNING: zero engine is not responding on 127.0.0.1:8642"
  echo "Check: journalctl --user -u zero-engine -n 50"
fi

say "done. Launch the app with: bash $CAN/launch-app.sh"
