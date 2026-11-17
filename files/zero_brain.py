"""Zero engine — the brain is the engine.

Registers ``zero`` in the OpenJarvis EngineRegistry as an OpenAI-compatible
endpoint served by ``~/zero-ai/runtime/zero_engine.py`` (the brain: persona +
hard rules + knowledge/ facts). No Ollama, no llama.cpp, no vLLM, no model
weights, no cloud provider.

Default endpoint: http://127.0.0.1:8642  (override with ZERO_ENGINE_HOST)
"""

from __future__ import annotations

import os

from openjarvis.core.registry import EngineRegistry
from openjarvis.engine._openai_compat import _OpenAICompatibleEngine


class ZeroEngine(_OpenAICompatibleEngine):
    """Answers from the local Zero brain, not from a language model."""

    engine_id = "zero"
    _default_host = os.environ.get("ZERO_ENGINE_HOST", "http://127.0.0.1:8642")
    _api_prefix = "/v1"


EngineRegistry.register("zero")(ZeroEngine)

__all__ = ["ZeroEngine"]
