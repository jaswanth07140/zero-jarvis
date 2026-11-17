"""Restrict /v1/models to the Zero model when the configured engine is zero.

The desktop app auto-selects the first model in /v1/models. The stock server
aggregates every discovered engine (NIM, Ollama, cloud...), which made the app
grab bigcode/starcoder2-15b instead of Zero. Idempotent.
"""
import pathlib
import sys

ROUTES = pathlib.Path.home() / "openjarvis/src/openjarvis/server/routes.py"
MARK = "# --- zero: only expose the Zero model"

if not ROUTES.exists():
    print(f"    routes.py not found at {ROUTES}", file=sys.stderr)
    sys.exit(1)

text = ROUTES.read_text(encoding="utf-8")
if MARK in text:
    print("    /v1/models already restricted to zero")
    sys.exit(0)

anchor = '''    from openjarvis.server.cloud_router import is_cloud_model, list_local_models
'''
inject = '''    from openjarvis.server.cloud_router import is_cloud_model, list_local_models

''' + MARK + '''
    try:
        from openjarvis.core.config import load_config as _zero_load_config

        if _zero_load_config().engine.default == "zero":
            return ModelListResponse(
                data=[ModelObject(id="zero", owned_by="openjarvis")]
            )
    except Exception:
        pass
'''

if anchor not in text:
    print("    anchor not found in routes.py", file=sys.stderr)
    sys.exit(1)

text = text.replace(anchor, inject, 1)
ROUTES.write_text(text, encoding="utf-8")
print("    /v1/models restricted to zero")
