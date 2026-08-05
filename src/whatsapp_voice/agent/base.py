"""The Agent seam.

`Agent` is what `AgentHandler` depends on, so the WhatsApp layer never imports
an LLM SDK. Swapping Claude for another provider, or dropping in a scripted
agent for tests, is a constructor change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentReply:
    """What the agent decided to say, plus what it took to get there."""

    text: str
    #: Names of tools called while answering, in order. Logged for cost and
    #: correctness review — "which API did it actually hit?"
    tools_used: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    #: True when the model declined the request (safety classifiers or its own
    #: refusal) rather than answering.
    refused: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AgentError(RuntimeError):
    """The agent could not produce a reply — API down, auth failure, loop limit."""


class Agent(ABC):
    @abstractmethod
    def answer(
        self, conversation_id: str, message: str, language: str | None = None
    ) -> AgentReply:
        """Answer one user message within a named conversation.

        `conversation_id` scopes history — use the sender's phone number so a
        follow-up like "and yesterday?" resolves against the right thread.

        `language` is the BCP-47 tag the speech engine detected (`ta-IN`,
        `hi-IN`, …). It goes into the system prompt rather than the message,
        because it describes *how* to reply, not what the user said — and
        because a hint inside the message text would be re-read as a user
        instruction on every later turn. None means "infer from the text",
        which is what a typed message gets.
        """

    def warmup(self) -> None:  # noqa: B027 - intentionally optional, not abstract
        """Optional one-time setup, called at server start."""


class ScriptedAgent(Agent):
    """A fixed-reply agent, for tests and for exercising the WhatsApp path
    without spending tokens.

    Also the fallback when no LLM key is configured, so the server still starts
    and explains itself instead of crashing.
    """

    def __init__(self, reply: str = "The agent is not configured yet.") -> None:
        self._reply = reply
        self.seen: list[tuple[str, str]] = []
        self.languages: list[str | None] = []

    def answer(
        self, conversation_id: str, message: str, language: str | None = None
    ) -> AgentReply:
        self.seen.append((conversation_id, message))
        self.languages.append(language)
        return AgentReply(text=self._reply)
