"""JarvisAgent — personal Jarvis persona agent (subclass of SimpleAgent)."""

from __future__ import annotations

from openjarvis.agents.simple import SimpleAgent
from openjarvis.core.registry import AgentRegistry


@AgentRegistry.register("jarvis")
class JarvisAgent(SimpleAgent):
    """Jarvis — calm, capable personal AI assistant, powered by the Zero engine."""

    agent_id = "jarvis"
    supports_managed_tool_fallback = True


__all__ = ["JarvisAgent"]
