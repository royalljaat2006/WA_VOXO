"""The Claude-backed agent: transcript in, answer out, tools called in between.

The loop is deliberately hand-written rather than using the SDK's tool runner,
for two reasons specific to this project: replies go out over WhatsApp (so we
need the final text, not a stream), and every tool call has to be recorded for
the audit trail a fintech deployment will want.

Model choice and the reasoning behind it live in docs/phase-4-agent.md.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..config import AgentSettings
from .base import Agent, AgentError, AgentReply
from .conversation import ConversationStore
from .formatting import for_whatsapp
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a reporting assistant for eko, reached over WhatsApp. Users speak to you \
in voice notes, usually in Hinglish (a mix of Hindi and English), and their words \
reach you as an automatic transcript.

How to answer:
- Reply in the same language mix the user used. If they wrote Hinglish, answer in \
Hinglish. Keep the register conversational, not formal.
- Keep replies short. This is a chat window, not a report — two or three sentences \
for most answers.
- Lead with the number or the answer, then any caveat. Not the other way round.
- WhatsApp is not markdown. It renders *single asterisks* as bold and ignores \
`##` headings, `**double asterisks`, and `-` bullet lists — those arrive as literal \
punctuation the user has to read past. Write figures into sentences instead: \
"2,841 bheje, 2,824 deliver hue — 99.4%". Only break into separate lines when \
listing four or more items, and start each with a plain "•".

What you are asked about:
- Most questions will be about eko — campaigns, delivery reports, transfers. \
Those need a tool.
- Some will be ordinary questions with nothing to do with eko ("India ki capital \
kya hai?", "5 kilo kitne pound hote hain?"). Answer those directly from what you \
know, in one short sentence. Do not call a tool, do not apologise, and do not \
tell the user you are only a reporting assistant — just answer and move on.
- If a question could be either ("kitne gaye?" with no subject), ask which one \
rather than guessing.

Using tools:
- Call a tool whenever the answer depends on live data. Never guess a number, a \
count, or a date from memory, and never state a figure a tool did not give you.
- If a request is ambiguous about which campaign, which date, or which range, ask \
one short clarifying question instead of picking for the user.
- The transcript may have misheard names. If a campaign name in the request does \
not match anything you can find, say what you looked for and ask them to confirm \
it — do not silently substitute the closest match.

Limits:
- You can read and report. You cannot send campaigns, change data, or take any \
action, and you should say so plainly if asked.
- If a tool fails, say what did not work in one sentence. Do not invent a fallback \
number.\
"""


#: BCP-47 tags the speech engine returns → the language's own name. Naming the
#: language beats passing the tag through: a model told "reply in ta-IN" is
#: guessing, one told "reply in Tamil" is not.
LANGUAGE_NAMES: dict[str, str] = {
    "hi": "Hindi", "en": "English", "bn": "Bengali", "gu": "Gujarati",
    "kn": "Kannada", "ml": "Malayalam", "mr": "Marathi", "od": "Odia",
    "or": "Odia", "pa": "Punjabi", "ta": "Tamil", "te": "Telugu",
    "as": "Assamese", "ur": "Urdu", "sa": "Sanskrit", "ks": "Kashmiri",
    "ne": "Nepali", "sd": "Sindhi", "kok": "Konkani", "doi": "Dogri",
    "mai": "Maithili", "mni": "Manipuri", "brx": "Bodo", "sat": "Santali",
}


def language_name(tag: str | None) -> str | None:
    """`ta-IN` → `Tamil`. None for anything unrecognised, including Sarvam's
    literal `unknown` — better to say nothing about language than to name the
    wrong one."""
    if not tag:
        return None
    return LANGUAGE_NAMES.get(tag.split("-")[0].strip().lower())


def system_prompt_for(
    today: date | None = None,
    base: str = SYSTEM_PROMPT,
    language: str | None = None,
) -> str:
    """`base` with today's date, and the user's language when it is known.

    The tools take ISO dates and the prompt tells the model to resolve "aaj" and
    "kal" itself — which it cannot do without knowing the date. Left out, a model
    guesses from its training data, the guess falls outside the data range, and
    it burns the whole tool-turn budget retrying. That is a real failure this
    project hit, not a hypothetical.

    Computed per request rather than at construction, so a server running past
    midnight does not keep yesterday's date. It only changes once a day, which
    keeps the prompt cacheable.
    """
    prompt = f"{base}\n\nToday's date is {(today or date.today()).isoformat()}."
    name = language_name(language)
    if name:
        prompt += (
            f"\n\nThe user is speaking {name}. Reply in {name}, in the script they "
            f"used. Keep English words that {name} speakers normally keep in English "
            "(product names, 'transfer', 'balance'); do not translate those."
        )
    return prompt


class ClaudeAgent(Agent):
    def __init__(
        self,
        settings: AgentSettings,
        tools: ToolRegistry,
        conversations: ConversationStore | None = None,
        client: Any = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if not settings.api_key and client is None:
            raise AgentError(
                "ANTHROPIC_API_KEY is not set — see docs/phase-4-agent.md"
            )
        self._settings = settings
        self._tools = tools
        self._conversations = conversations or ConversationStore(
            max_turns=settings.max_history_turns,
            idle_timeout_s=settings.conversation_idle_timeout_s,
        )
        self._system_prompt = system_prompt
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self._settings.api_key,
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

        # Bounded loop: each pass is one model turn plus any tool calls it made.
        # The cap stops a tool that keeps failing from looping until the bill
        # notices.
        for turn in range(self._settings.max_tool_turns):
            response = self._call_model(conversation_id, language)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens

            if getattr(response, "stop_reason", None) == "refusal":
                logger.warning("Model refused for %s", conversation_id)
                self._conversations.reset(conversation_id)
                return AgentReply(
                    text="I can't help with that one. Try asking about campaigns or reports.",
                    tools_used=tuple(tools_used),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    refused=True,
                )

            # Echo the assistant turn back verbatim — tool_use blocks included,
            # or the follow-up request has no record of what was called.
            thread.append("assistant", response.content)

            tool_calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_calls:
                return AgentReply(
                    text=self._text_of(response),
                    tools_used=tuple(tools_used),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

            results = []
            for call in tool_calls:
                tools_used.append(call.name)
                output, is_error = self._tools.run(call.name, dict(call.input))
                logger.info("Tool %s → %s%s", call.name, output[:200],
                            " [error]" if is_error else "")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                    "is_error": is_error,
                })

            # All results go back in ONE user message. Splitting them teaches the
            # model to stop making parallel calls.
            thread.append("user", results)

            logger.debug("Turn %d used tools: %s", turn + 1, [c.name for c in tool_calls])

        logger.error("Hit max_tool_turns (%d) for %s", self._settings.max_tool_turns,
                     conversation_id)
        return AgentReply(
            text="That took more steps than I could complete. Could you ask it more simply?",
            tools_used=tuple(tools_used),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ------------------------------------------------------------- internals

    def _call_model(self, conversation_id: str, language: str | None = None) -> Any:
        request: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": self._settings.max_tokens,
            "system": system_prompt_for(base=self._system_prompt, language=language),
            "messages": self._conversations.history_for_request(conversation_id),
            "output_config": {"effort": self._settings.effort},
        }
        if len(self._tools):
            request["tools"] = self._tools.definitions()

        try:
            return self.client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - SDK raises a family of types
            raise AgentError(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _text_of(response: Any) -> str:
        parts = [
            block.text for block in response.content
            if getattr(block, "type", None) == "text" and block.text.strip()
        ]
        return for_whatsapp("\n\n".join(parts)) or "I don't have an answer for that."
