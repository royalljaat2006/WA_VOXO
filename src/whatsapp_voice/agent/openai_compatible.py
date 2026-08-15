"""The agent for any provider that speaks OpenAI's chat-completions dialect.

Two do: NVIDIA NIM (`nemotron_agent.py`) and Google Gemini (`gemini_agent.py`).
Both are thin subclasses that answer three questions — which key, which URL,
which model — and inherit everything else, because everything else is genuinely
identical. The alternative was a second copy of the tool loop per provider,
which is how two implementations quietly drift apart.

This is **not** a base class for `ClaudeAgent`. Anthropic's wire format differs
in ways that are not cosmetic:

| | Anthropic | OpenAI-compatible |
|---|---|---|
| System prompt | separate `system` field | first message in the list |
| Tool schema | flat `{name, …, input_schema}` | nested under `{"type":"function", …}` |
| Tool args | parsed dict | **JSON string** to parse |
| Tool results | ALL in one `user` message | one `tool` message **per call** |
| Sampling | `temperature` rejected | `temperature` accepted |

Getting the tool-result shape wrong is the subtle one: Anthropic wants a single
user message carrying every result, OpenAI wants one message per `tool_call_id`.
Sending Anthropic's shape here silently loses results.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import AgentSettings
from .base import Agent, AgentError, AgentReply
from .claude_agent import SYSTEM_PROMPT, system_prompt_for
from .conversation import ConversationStore
from .formatting import for_whatsapp
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


class OpenAICompatibleAgent(Agent):
    """Shared tool loop. Subclasses supply the three provider-specific values."""

    #: Named in the error raised when the key is missing, so an operator is told
    #: which variable to set rather than "the API key".
    key_setting_name: str = "API key"
    #: Where to read about configuring this provider.
    setup_doc: str = "docs/phase-4-agent.md"

    def __init__(
        self,
        settings: AgentSettings,
        tools: ToolRegistry,
        conversations: ConversationStore | None = None,
        client: Any = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if not self.api_key_from(settings) and client is None:
            raise AgentError(
                f"{self.key_setting_name} is not set — see {self.setup_doc}"
            )
        self._settings = settings
        self._tools = tools
        self._conversations = conversations or ConversationStore(
            max_turns=settings.max_history_turns,
            idle_timeout_s=settings.conversation_idle_timeout_s,
        )
        self._system_prompt = system_prompt
        self._client = client

    # ------------------------------------------------------- provider hooks

    @staticmethod
    def api_key_from(settings: AgentSettings) -> str | None:
        raise NotImplementedError

    @staticmethod
    def base_url_from(settings: AgentSettings) -> str:
        raise NotImplementedError

    @staticmethod
    def model_from(settings: AgentSettings) -> str:
        raise NotImplementedError

    # -------------------------------------------------------------- client

    @property
    def client(self) -> Any:
        if self._client is None:
            # The OpenAI SDK speaks every one of these dialects natively — only
            # base_url and the key differ.
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key_from(self._settings),
                base_url=self.base_url_from(self._settings),
                timeout=self._settings.request_timeout_s,
            )
        return self._client

    def warmup(self) -> None:
        _ = self.client  # surface a bad key at startup, not mid-conversation

    # ------------------------------------------------------------------ main

    def answer(
        self, conversation_id: str, message: str, language: str | None = None
    ) -> AgentReply:
        thread = self._conversations.get(conversation_id)
        thread.append("user", message)

        tools_used: list[str] = []
        input_tokens = output_tokens = 0

        for turn in range(self._settings.max_tool_turns):
            response = self._call_model(conversation_id, language)

            usage = getattr(response, "usage", None)
            if usage:
                input_tokens += usage.prompt_tokens or 0
                output_tokens += usage.completion_tokens or 0

            choice = response.choices[0]
            reply_message = choice.message
            calls = list(getattr(reply_message, "tool_calls", None) or [])

            # Echo the assistant turn back in the shape the API expects on the
            # next request — tool_calls included, or it has no record of what it
            # asked for.
            thread.append(
                "assistant",
                reply_message.content or "",
                extra={
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.function.name,
                                "arguments": c.function.arguments,
                            },
                        }
                        for c in calls
                    ]
                }
                if calls
                else None,
            )

            if not calls:
                return AgentReply(
                    text=for_whatsapp(reply_message.content or "")
                    or "I don't have an answer for that.",
                    tools_used=tuple(tools_used),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

            for call in calls:
                tools_used.append(call.function.name)
                arguments, parse_error = self._parse_arguments(call.function.arguments)

                if parse_error:
                    output, is_error = parse_error, True
                else:
                    output, is_error = self._tools.run(call.function.name, arguments)

                logger.info(
                    "Tool %s → %s%s",
                    call.function.name, output[:200], " [error]" if is_error else "",
                )
                # One `tool` message per call — NOT all results in one message.
                thread.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )

            logger.debug("Turn %d used tools: %s", turn + 1,
                         [c.function.name for c in calls])

        logger.error("Hit max_tool_turns (%d) for %s",
                     self._settings.max_tool_turns, conversation_id)
        return AgentReply(
            text="That took more steps than I could complete. Could you ask it more simply?",
            tools_used=tuple(tools_used),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
        """Tool arguments arrive as a JSON *string* here, unlike Anthropic.

        A malformed one is the model's mistake to correct, so it comes back as a
        readable tool error rather than crashing the turn.
        """
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            return {}, f"Your tool arguments were not valid JSON ({exc}). Try again."
        if not isinstance(parsed, dict):
            return {}, "Tool arguments must be a JSON object."
        return parsed, None

    def _call_model(self, conversation_id: str, language: str | None = None) -> Any:
        messages = [
            {"role": "system", "content": system_prompt_for(
                base=self._system_prompt, language=language
            )},
            *self._conversations.history_for_request(conversation_id),
        ]

        request: dict[str, Any] = {
            "model": self.model_from(self._settings),
            "max_tokens": self._settings.max_tokens,
            "temperature": self._settings.temperature,
            "messages": messages,
        }
        if len(self._tools):
            request["tools"] = [
                {"type": "function", "function": {
                    "name": definition["name"],
                    "description": definition["description"],
                    "parameters": definition["input_schema"],
                }}
                for definition in self._tools.definitions()
            ]

        try:
            return self.client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - SDK raises a family of types
            raise AgentError(f"{type(exc).__name__}: {exc}") from exc
