#!/usr/bin/env python
"""Prove the configured LLM works, and time a real tool call, before switching to it.

Answers the two questions that matter and that unit tests cannot: does the key
authenticate, and does the model emit a *correctly formed* tool call? A model
that replies fast but mangles its tool arguments is worse than one that is slow
— that is how `nemotron-3-nano` looked good for three seconds. And a model that
answers without calling a tool would never prepare a transfer at all.

    .venv/bin/python tools/check_agent.py              # whatever AGENT_PROVIDER says
    .venv/bin/python tools/check_agent.py --provider claude
    AGENT_MODEL=claude-opus-5 .venv/bin/python tools/check_agent.py -p claude

Costs one request on whichever provider is checked.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Windows' console codepage can't encode ✓/✗, which would crash every print
# before the probe reports anything. Same fix as the other tools/ scripts.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from whatsapp_voice.agent.base import Agent  # noqa: E402
from whatsapp_voice.agent.payment_tools import PAYMENT_PROMPT  # noqa: E402
from whatsapp_voice.agent.tools import Tool, ToolRegistry  # noqa: E402
from whatsapp_voice.config import AgentSettings, Settings  # noqa: E402

#: What a real user would say. Deliberately fake digits — this file is
#: version-controlled, so no real payee belongs in it.
PROBE = "9000000000@upi ko 200 rupaye bhejo"
EXPECTED_VPA = "9000000000@upi"


class FakeResolveTool(Tool):
    """Stands in for the real `resolve_payee`, so nothing is looked up and no
    gateway is involved — this is a probe, not a transfer."""

    name = "resolve_payee"
    description = (
        "Look up who a UPI ID (VPA) belongs to, before sending money. Accepts "
        "any UPI ID of the form 'something@handle'."
    )
    input_schema = {
        "type": "object",
        "properties": {"payee": {"type": "string"}},
        "required": ["payee"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, payee: str) -> str:
        self.calls.append(payee)
        return "vpa=9000000000@upi\nphone=9000000000\nis_new=true\nname=UNKNOWN"


def build(settings: AgentSettings, tools: ToolRegistry) -> Agent:
    """The selected provider's agent, without the campaign tools or a gateway.

    Deliberately not `build_agent()`: that wires in a data source and the real
    payment stack, and this script must not be able to touch either.
    """
    prompt = "You are a payments assistant." + PAYMENT_PROMPT

    if settings.provider == "claude":
        from whatsapp_voice.agent.claude_agent import ClaudeAgent

        return ClaudeAgent(settings, tools, system_prompt=prompt)
    if settings.provider == "nemotron":
        from whatsapp_voice.agent.nemotron_agent import NemotronAgent

        return NemotronAgent(settings, tools, system_prompt=prompt)
    if settings.provider == "gemini":
        from whatsapp_voice.agent.gemini_agent import GeminiAgent

        return GeminiAgent(settings, tools, system_prompt=prompt)

    print(f"✗ Unknown AGENT_PROVIDER={settings.provider!r}.")
    print("  Valid values: claude, nemotron, gemini.")
    raise SystemExit(1)


#: Where to get a key, per provider — printed when one is missing.
KEY_HELP = {
    "claude": ("ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
    "nemotron": ("NVIDIA_API_KEY", "https://build.nvidia.com"),
    "gemini": ("GEMINI_API_KEY", "https://aistudio.google.com/apikey"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-p", "--provider", choices=sorted(KEY_HELP),
        help="Override AGENT_PROVIDER for this check only.",
    )
    args = parser.parse_args()

    settings = Settings.load().agent
    if args.provider:
        settings = replace_provider(settings, args.provider)

    if not settings.is_configured:
        variable, url = KEY_HELP[settings.provider]
        print(f"✗ {variable} is not set (AGENT_PROVIDER={settings.provider}).")
        print(f"  Get a key at {url}")
        return 1

    print(f"provider : {settings.provider}")
    print(f"model    : {settings.active_model}")
    print(f"max_tokens: {settings.max_tokens}\n")

    tool = FakeResolveTool()
    agent = build(settings, ToolRegistry([tool]))

    started = time.monotonic()
    try:
        reply = agent.answer("probe", PROBE)
    except Exception as exc:  # noqa: BLE001 - this script exists to report failures
        print(f"✗ {type(exc).__name__}: {exc}")
        return 1
    took = time.monotonic() - started

    print(f"took     : {took:.1f}s")
    print(f"tools    : {list(reply.tools_used) or 'NONE'}")
    print(f"tokens   : {reply.input_tokens} in / {reply.output_tokens} out")
    print(f"reply    : {reply.text[:300]}\n")

    if reply.refused:
        print("✗ The model refused the request.")
        return 1

    if not tool.calls:
        print("✗ The model answered without calling resolve_payee.")
        print("  It would never prepare a transfer. Try another model.")
        return 1

    payee = tool.calls[0]
    if payee.strip() != EXPECTED_VPA:
        # The nano-model failure: plausible-looking arguments with template
        # fragments welded on. Silent, and catastrophic with real money.
        print(f"✗ Tool argument came through malformed: {payee!r}")
        print(f"  Expected {EXPECTED_VPA!r}. Do not use this model for payments.")
        return 1

    print(f"✓ Clean tool call: resolve_payee(payee={payee!r})")
    print(f"  Switch with: AGENT_PROVIDER={settings.provider}")
    return 0


def replace_provider(settings: AgentSettings, provider: str) -> AgentSettings:
    """`--provider` without re-reading the environment.

    `dataclasses.replace` rather than mutation: AgentSettings is shared with
    whatever else has already read it in this process.
    """
    from dataclasses import replace

    return replace(settings, provider=provider)


if __name__ == "__main__":
    raise SystemExit(main())
