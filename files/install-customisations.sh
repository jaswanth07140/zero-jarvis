#!/bin/bash
# Install/refresh the Zero customisations into the OpenJarvis checkout + venv.
# Idempotent. Safe to run after any update.
set -uo pipefail

CANON="/usr/local/share/zero-jarvis/canonical"
SRC="$HOME/openjarvis/src/openjarvis"
VENV_PY="$HOME/.openjarvis-venv/bin/python"
CONFIG_DIR="$HOME/.openjarvis"

[ -d "$SRC" ] || { echo "    no source tree at $SRC"; exit 1; }

# --- 1. Zero engine module (registers "zero" in the EngineRegistry) ----------
mkdir -p "$SRC/engine"
cp "$CANON/zero_brain.py" "$SRC/engine/zero_brain.py"

"$VENV_PY" - <<'PY'
import pathlib
p = pathlib.Path.home() / "openjarvis/src/openjarvis/engine/__init__.py"
text = p.read_text(encoding="utf-8")
if "zero_brain" not in text:
    text = text.replace(
        "import openjarvis.engine.openai_compat_engines  # noqa: F401",
        "import openjarvis.engine.openai_compat_engines  # noqa: F401\n"
        "import openjarvis.engine.zero_brain  # noqa: F401",
    )
    p.write_text(text, encoding="utf-8")
    print("    engine/__init__.py patched")
PY

# --- 2. jarvis agent --------------------------------------------------------
cp "$CANON/openjarvis-jarvis-agent.py" "$SRC/agents/jarvis.py"
"$VENV_PY" - <<'PY'
import pathlib
p = pathlib.Path.home() / "openjarvis/src/openjarvis/agents/__init__.py"
text = p.read_text(encoding="utf-8")
if "agents.jarvis" not in text:
    # Append as its own guarded import — never splice into an existing try block.
    text += (
        "\ntry:\n"
        "    import openjarvis.agents.jarvis  # noqa: F401\n"
        "except ImportError:\n"
        "    pass\n"
    )
    p.write_text(text, encoding="utf-8")
    print("    agents/__init__.py patched")
PY

# --- 2b. only advertise Zero in /v1/models ----------------------------------
"$VENV_PY" "$CANON/patch-models.py" || true

# --- 3. config: engine = zero, model = zero ---------------------------------
mkdir -p "$CONFIG_DIR"
cp "$CANON/config.toml" "$CONFIG_DIR/config.toml"

# --- 4. desktop app: point it at Zero, skip the setup screen ----------------
cp "$CANON/inference.json" "$CONFIG_DIR/inference.json"

# --- 5. no Ollama anywhere --------------------------------------------------
if [ -f /usr/bin/ollama ]; then
  sudo rm -f /usr/bin/ollama 2>/dev/null && echo "    removed bundled ollama"
fi

echo "    customisations installed (engine: zero)"
