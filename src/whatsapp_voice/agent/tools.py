"""Tools the agent can call, and the registry that exposes them to the LLM.

A `Tool` is the seam between "the model decided to do something" and "our code
does it". Each tool declares a JSON Schema for its inputs; the registry turns
that into the wire format Claude expects and dispatches calls back to Python.

Adding a capability means adding a Tool subclass and registering it — never
adding a branch inside the agent loop.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A tool could not complete. The message is shown to the model, which may
    retry differently or explain the problem to the user, so write it for a
    reader — not as a stack trace."""


class Tool(ABC):
    #: Stable identifier the model uses to call this tool. snake_case.
    name: str
    #: What the tool does AND when to call it. The "when" matters as much as the
    #: "what" — it is the main lever on whether the model reaches for it.
    description: str
    #: JSON Schema for the inputs. Keep `additionalProperties: false` and list
    #: `required` so `strict` validation can be switched on.
    input_schema: dict[str, Any]

    @abstractmethod
    def run(self, **kwargs: Any) -> str:
        """Execute the tool and return a result string for the model to read.

        Return data, not prose — the model writes the user-facing sentence. Raise
        ToolError for expected failures (not found, bad range, upstream down).
        """

    def definition(self) -> dict[str, Any]:
        """The tool in the shape the Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:
        return f"<Tool {self.name}>"


class ToolRegistry:
    """The set of tools available for one agent, keyed by name."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions for the API request.

        Sorted by name so the serialised tool list is byte-identical between
        requests — a varying tool order would invalidate the prompt cache.
        """
        return [self._tools[name].definition() for name in sorted(self._tools)]

    def run(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Dispatch one tool call. Returns (result_text, is_error).

        Never raises: a tool failure has to travel back to the model as a
        readable tool_result, otherwise the conversation stalls with no
        explanation for the user.
        """
        tool = self._tools.get(name)
        if tool is None:
            logger.error("Model called unknown tool %r", name)
            return (f"No such tool: {name}. Available: {', '.join(sorted(self._tools))}", True)

        try:
            return (tool.run(**arguments), False)
        except ToolError as exc:
            logger.info("Tool %s declined: %s", name, exc)
            return (str(exc), True)
        except TypeError as exc:
            # Wrong or missing arguments — the model can correct this if told.
            logger.warning("Tool %s called with bad arguments %s: %s", name, arguments, exc)
            return (f"Invalid arguments for {name}: {exc}", True)
        except Exception:
            logger.exception("Tool %s raised unexpectedly", name)
            return (f"{name} failed unexpectedly. Tell the user it could not be completed.", True)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
