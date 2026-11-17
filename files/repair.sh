#!/usr/bin/env bash
# Local repair — no network, no clone, no credentials.
#
# Restores Zero as the engine for the OpenJarvis app using the canonical
# copies already on this machine (/usr/local/share/zero-jarvis/canonical).
# Safe to run any time: after a stock OpenJarvis install, after a .deb
# upgrade, or if the app shows the wrong model.
set -uo pipefail

CAN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\033[1;36m>>> %s\033[0m\n' "$*"; }

say "1/4 restoring files (brain, engine, agent, config, uv shim)"
bash "$CAN/restore.sh"

say "2/4 restarting the zero engine"
mkdir -p "$HOME/.config/systemd/user"
cp "$CAN/zero-engine.service" "$HOME/.config/systemd/user/" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable --now zero-engine.service 2>/dev/null || true
systemctl --user restart zero-engine.service 2>/dev/null || true

say "3/4 ensuring the self-healing guard is armed"
sudo cp "$CAN/zero-guard.service" "$CAN/zero-guard.timer" /etc/systemd/system/ 2>/dev/null || true
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl enable --now zero-guard.timer 2>/dev/null || true

sleep 2
if curl -sf --max-time 5 http://127.0.0.1:8642/health >/dev/null; then
  say "engine OK: $(curl -s http://127.0.0.1:8642/health | head -c 140)"
else
  echo "WARNING: engine not responding — journalctl --user -u zero-engine -n 50"
fi

say "4/4 launching the app"
exec bash "$CAN/launch-app.sh"
