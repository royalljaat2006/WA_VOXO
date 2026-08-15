"""NVIDIA Nemotron agent, over NIM's OpenAI-compatible API.

A sibling of `ClaudeAgent`, not a replacement. Both implement `Agent`, so the
WhatsApp and demo layers cannot tell them apart, and both reuse the same
`SYSTEM_PROMPT`, the same `ToolRegistry`, and the same `ConversationStore`.

The tool loop and the wire-format notes live in `openai_compatible.py`, shared
with `GeminiAgent`. Only the three values below are NVIDIA-specific.

⚠️ **A model being listed in NIM's `/v1/models` does not mean it answers.**
`nemotron-3-ultra-550b-a55b` was the default here until it stopped responding
entirely — still catalogued, but every request hangs until the client gives up.
Nothing errors, so the symptom is a silent bot. See the model table in
`docs/voice-payments.md`, and re-measure rather than trusting it.
"""

from __future__ import annotations

from ..config import AgentSettings
from .openai_compatible import OpenAICompatibleAgent


class NemotronAgent(OpenAICompatibleAgent):
    key_setting_name = "NVIDIA_API_KEY"
    setup_doc = "docs/phase-4-agent.md"

    @staticmethod
    def api_key_from(settings: AgentSettings) -> str | None:
        return settings.nvidia_api_key

    @staticmethod
    def base_url_from(settings: AgentSettings) -> str:
        return settings.nvidia_base_url

    @staticmethod
    def model_from(settings: AgentSettings) -> str:
        return settings.nemotron_model
