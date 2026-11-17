# zero-jarvis

Zero is the engine for the OpenJarvis desktop app — **no Ollama, no llama.cpp,
no vLLM, no model weights, no cloud provider**.

The brain lives in [`zero-ai`](https://github.com/jaswanth07140/zero-ai):
persona + hard rules + `knowledge/` facts. This repo is the small (~100 KB)
install layer that wires that brain into OpenJarvis and puts it back when a
stock OpenJarvis install overwrites things.

## What it does

```
OpenJarvis desktop app
        │  uv run jarvis serve --port 8000 --engine zero --model zero
        ▼
jarvis serve (Python, ~/.openjarvis-venv)
        │  OpenAI-compatible client
        ▼
zero engine (127.0.0.1:8642)
        │
        ▼
~/zero-ai  identity/ + knowledge/   ← the brain, retrieval only
```

- **Engine** — `src/openjarvis/engine/zero_brain.py` registers `zero`.
- **Agent** — `src/openjarvis/agents/jarvis.py` registers `jarvis`.
- **Models** — `/v1/models` is restricted to `zero` so the app cannot pick a
  leftover NIM/Ollama model.
- **uv shim** — the packaged app calls `uv sync …` and a Rust import check;
  neither is needed here, so `~/.local/bin/uv` short-circuits them.
- **Guard** — `zero-guard.timer` re-applies everything every minute, because
  files under `$HOME` on this machine have a habit of disappearing.

## Install / repair

```bash
git clone https://github.com/jaswanth07140/zero-jarvis.git ~/zero-jarvis
bash ~/zero-jarvis/install.sh
```

Run it again any time — after `curl -fsSL open-jarvis.github.io/OpenJarvis/install.sh | bash`,
after a `.deb` upgrade, or after a restart ate the config.

## Launch

```bash
bash /usr/local/share/zero-jarvis/canonical/launch-app.sh
```

## Notes

- The engine answers by retrieval only. An unmatched question returns
  *"That is not recorded, so I will not guess."* plus the closest domains —
  that is intended, not a failure.
- `host` must be `http://127.0.0.1:8642` (the app strips a trailing `/v1`).
- Engine logs: `journalctl --user -u zero-engine -f`
- Guard log: `/usr/local/share/zero-jarvis/restore.log`
