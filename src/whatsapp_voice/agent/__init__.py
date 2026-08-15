"""The agent: transcript in, answer out, internal APIs called in between."""

from .base import Agent, AgentError, AgentReply, ScriptedAgent
from .campaign_tools import campaign_tools
from .claude_agent import SYSTEM_PROMPT, ClaudeAgent, system_prompt_for
from .conversation import Conversation, ConversationStore
from .datasource import (
    CampaignCount,
    CampaignDataSource,
    DataSourceError,
    DeliveryReport,
    StubCampaignSource,
)
from .factory import build_agent
from .formatting import for_whatsapp
from .gemini_agent import GeminiAgent
from .nemotron_agent import NemotronAgent
from .openai_compatible import OpenAICompatibleAgent
from .tools import Tool, ToolError, ToolRegistry

__all__ = [
    "SYSTEM_PROMPT",
    "Agent",
    "AgentError",
    "AgentReply",
    "CampaignCount",
    "CampaignDataSource",
    "ClaudeAgent",
    "Conversation",
    "ConversationStore",
    "DataSourceError",
    "DeliveryReport",
    "GeminiAgent",
    "NemotronAgent",
    "OpenAICompatibleAgent",
    "ScriptedAgent",
    "StubCampaignSource",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "build_agent",
    "campaign_tools",
    "for_whatsapp",
    "system_prompt_for",
]
