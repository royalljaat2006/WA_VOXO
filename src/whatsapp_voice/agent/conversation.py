"""Conversation history, so follow-ups work.

Without this, "aur kal ka?" has no antecedent and the agent has to ask what the
user means every single time.

In-memory and bounded. That is the right amount of machinery for a single-process
demo; a multi-worker deployment needs shared storage, since each worker would
otherwise hold its own copy and follow-ups would land on whichever worker
happened to answer. Noted in docs/phase-6-deploy.md as a deployment prerequisite.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Conversation:
    """One thread of messages, in the shape the Messages API wants.

    Entries are whole message dicts (`{"role": ..., "content": ...}`) rather than
    plain strings, because tool_use and tool_result blocks have to be replayed
    verbatim for the model to make sense of its own earlier turns.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)

    def append(self, role: str, content: Any,
               extra: dict[str, Any] | None = None) -> None:
        """Add a message. `extra` carries provider-specific keys.

        The OpenAI-compatible dialect puts `tool_calls` alongside `content` on an
        assistant message, where Anthropic puts them inside `content`. Rather
        than teach this store either format, it takes whatever the caller needs
        to replay verbatim.
        """
        message: dict[str, Any] = {"role": role, "content": content}
        if extra:
            message.update(extra)
        self.messages.append(message)
        self.last_active = time.monotonic()


class ConversationStore:
    """Recent conversations, keyed by conversation id (the sender's number).

    Two independent limits:
      - `max_turns` caps how much history one conversation carries into a
        request, which bounds per-message token cost.
      - `idle_timeout_s` drops threads nobody has touched, so yesterday's
        context doesn't silently attach itself to a fresh question.
    """

    def __init__(
        self,
        max_turns: int = 20,
        idle_timeout_s: float = 3600.0,
        capacity: int = 500,
    ) -> None:
        self._max_turns = max_turns
        self._idle_timeout_s = idle_timeout_s
        self._capacity = capacity
        self._threads: OrderedDict[str, Conversation] = OrderedDict()

    def get(self, conversation_id: str) -> Conversation:
        """The live conversation for this id, creating or resetting as needed."""
        thread = self._threads.get(conversation_id)

        if thread is not None and self._is_idle(thread):
            thread = None  # too old to be context; start fresh

        if thread is None:
            thread = Conversation()
            self._threads[conversation_id] = thread

        self._threads.move_to_end(conversation_id)
        self._evict_if_over_capacity()
        return thread

    def history_for_request(self, conversation_id: str) -> list[dict[str, Any]]:
        """The trimmed message list to send to the model.

        Trimming keeps the *end* of the conversation — recent turns matter more
        than the opening. The window starts at a `user` message because the API
        rejects a history that opens on an assistant turn.
        """
        messages = self.get(conversation_id).messages
        if len(messages) <= self._max_turns:
            return list(messages)

        window = messages[-self._max_turns:]
        first_user = next((i for i, m in enumerate(window) if m["role"] == "user"), None)
        return [] if first_user is None else window[first_user:]

    def reset(self, conversation_id: str) -> None:
        self._threads.pop(conversation_id, None)

    def _is_idle(self, thread: Conversation) -> bool:
        return (time.monotonic() - thread.last_active) > self._idle_timeout_s

    def _evict_if_over_capacity(self) -> None:
        while len(self._threads) > self._capacity:
            self._threads.popitem(last=False)  # oldest first

    def __len__(self) -> int:
        return len(self._threads)
