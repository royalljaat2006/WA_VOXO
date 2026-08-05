"""Nemotron agent tests. No network — a fake client returns responses in the
OpenAI-compatible shape NVIDIA NIM produces.

The wire-format differences from Anthropic get the most coverage, because they
are the parts that are easy to get subtly wrong and hard to notice.
"""

from types import SimpleNamespace

import pytest

from whatsapp_voice.agent.base import AgentError
from whatsapp_voice.agent.nemotron_agent import NemotronAgent
from whatsapp_voice.agent.tools import Tool, ToolRegistry
from whatsapp_voice.config import AgentSettings


def function_call(name, arguments, call_id="call-1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def completion(content=None, tool_calls=None, prompt_tokens=100, completion_tokens=20):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason="tool_calls" if tool_calls else "stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("more model calls than scripted responses")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))

    @property
    def requests(self):
        return self.chat.completions.requests


class CountTool(Tool):
    name = "get_campaign_count"
    description = "count campaigns on a date"
    input_schema = {
        "type": "object",
        "properties": {"on": {"type": "string"}},
        "required": ["on"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.calls = []

    def run(self, on):
        self.calls.append(on)
        return f"date={on}\ntotal_campaigns=12"


@pytest.fixture
def settings():
    return AgentSettings(provider="nemotron", nvidia_api_key="nvapi-test",
                         max_tool_turns=4)


def make_agent(responses, settings, tool=None):
    tool = tool if tool is not None else CountTool()
    client = FakeClient(responses)
    agent = NemotronAgent(settings, ToolRegistry([tool]), client=client)
    return agent, tool, client


class TestDirectAnswer:
    def test_returns_the_content(self, settings):
        agent, _, _ = make_agent([completion(content="Namaste!")], settings)
        assert agent.answer("u1", "hello").text == "Namaste!"

    def test_accumulates_usage(self, settings):
        agent, _, _ = make_agent(
            [completion(content="hi", prompt_tokens=150, completion_tokens=30)], settings
        )
        reply = agent.answer("u1", "hello")
        assert (reply.input_tokens, reply.output_tokens) == (150, 30)

    def test_empty_content_gets_a_fallback(self, settings):
        agent, _, _ = make_agent([completion(content="")], settings)
        assert agent.answer("u1", "hi").text == "I don't have an answer for that."

    def test_markdown_is_normalised_for_whatsapp(self, settings):
        agent, _, _ = make_agent([completion(content="Aaj **18** gaye")], settings)
        assert agent.answer("u1", "hi").text == "Aaj *18* gaye"


class TestWireFormat:
    def test_system_prompt_is_the_first_message(self, settings):
        """Anthropic takes a separate `system` field; this dialect does not."""
        agent, _, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")

        request = client.requests[0]
        assert "system" not in request
        assert request["messages"][0]["role"] == "system"
        assert "reporting assistant for eko" in request["messages"][0]["content"]

    def test_system_prompt_carries_todays_date(self, settings):
        from datetime import date

        agent, _, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert date.today().isoformat() in client.requests[0]["messages"][0]["content"]

    def test_tools_are_nested_under_function(self, settings):
        agent, _, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")

        tool = client.requests[0]["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_campaign_count"
        # `parameters`, not `input_schema`, in this dialect.
        assert "parameters" in tool["function"]

    def test_temperature_is_sent(self, settings):
        """The inverse of Claude, which rejects it."""
        agent, _, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert client.requests[0]["temperature"] == settings.temperature

    def test_uses_the_nemotron_model_id(self, settings):
        agent, _, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert client.requests[0]["model"] == "nvidia/nemotron-3-ultra-550b-a55b"

    def test_omits_tools_when_none_registered(self, settings):
        client = FakeClient([completion(content="hi")])
        NemotronAgent(settings, ToolRegistry(), client=client).answer("u1", "hi")
        assert "tools" not in client.requests[0]


class TestToolLoop:
    def test_parses_json_string_arguments(self, settings):
        """Arguments arrive as a JSON string here, not a dict."""
        agent, tool, _ = make_agent([
            completion(tool_calls=[function_call("get_campaign_count", '{"on":"2026-07-28"}')]),
            completion(content="Aaj 12 gaye."),
        ], settings)

        reply = agent.answer("u1", "aaj kitne?")

        assert tool.calls == ["2026-07-28"]
        assert reply.text == "Aaj 12 gaye."
        assert reply.tools_used == ("get_campaign_count",)

    def test_each_result_is_its_own_tool_message(self, settings):
        """OpenAI wants one `tool` message per call — not all in one message."""
        agent, _, client = make_agent([
            completion(tool_calls=[
                function_call("get_campaign_count", '{"on":"2026-07-28"}', "c1"),
                function_call("get_campaign_count", '{"on":"2026-07-27"}', "c2"),
            ]),
            completion(content="done"),
        ], settings)

        agent.answer("u1", "aaj aur kal?")

        tool_messages = [
            m for m in client.requests[1]["messages"] if m["role"] == "tool"
        ]
        assert len(tool_messages) == 2
        assert {m["tool_call_id"] for m in tool_messages} == {"c1", "c2"}

    def test_assistant_turn_replays_its_tool_calls(self, settings):
        agent, _, client = make_agent([
            completion(tool_calls=[function_call("get_campaign_count", '{"on":"2026-07-28"}')]),
            completion(content="done"),
        ], settings)

        agent.answer("u1", "aaj kitne?")

        assistant = [m for m in client.requests[1]["messages"] if m["role"] == "assistant"]
        assert assistant[0]["tool_calls"][0]["function"]["name"] == "get_campaign_count"

    def test_malformed_json_comes_back_as_a_tool_error(self, settings):
        agent, tool, client = make_agent([
            completion(tool_calls=[function_call("get_campaign_count", "{not json")]),
            completion(content="Sorry, I couldn't look that up."),
        ], settings)

        agent.answer("u1", "aaj kitne?")

        assert tool.calls == [], "a malformed call must not reach the tool"
        tool_message = next(m for m in client.requests[1]["messages"] if m["role"] == "tool")
        assert "not valid JSON" in tool_message["content"]

    def test_non_object_arguments_rejected(self, settings):
        agent, tool, client = make_agent([
            completion(tool_calls=[function_call("get_campaign_count", '"a string"')]),
            completion(content="hmm"),
        ], settings)

        agent.answer("u1", "aaj kitne?")

        assert tool.calls == []
        tool_message = next(m for m in client.requests[1]["messages"] if m["role"] == "tool")
        assert "must be a JSON object" in tool_message["content"]

    def test_unknown_tool_is_reported_back(self, settings):
        agent, _, client = make_agent([
            completion(tool_calls=[function_call("no_such_tool", "{}")]),
            completion(content="I couldn't do that."),
        ], settings)

        agent.answer("u1", "hi")

        tool_message = next(m for m in client.requests[1]["messages"] if m["role"] == "tool")
        assert "No such tool" in tool_message["content"]

    def test_stops_at_max_tool_turns(self, settings):
        looping = [
            completion(tool_calls=[function_call("get_campaign_count", '{"on":"2026-07-28"}')])
            for _ in range(4)
        ]
        agent, _, _ = make_agent(looping, settings)

        reply = agent.answer("u1", "loop")

        assert "more steps than I could complete" in reply.text
        assert len(reply.tools_used) == 4


class TestErrors:
    def test_api_failure_raises_agent_error(self, settings):
        agent, _, _ = make_agent([RuntimeError("connection reset")], settings)
        with pytest.raises(AgentError, match="RuntimeError: connection reset"):
            agent.answer("u1", "hi")

    def test_missing_key_rejected_at_construction(self):
        with pytest.raises(AgentError, match="NVIDIA_API_KEY"):
            NemotronAgent(AgentSettings(provider="nemotron"), ToolRegistry())


class TestConversation:
    def test_follow_up_carries_history(self, settings):
        agent, _, client = make_agent([
            completion(content="Aaj 12 gaye."),
            completion(content="Kal 9 gaye."),
        ], settings)

        agent.answer("u1", "aaj kitne?")
        agent.answer("u1", "aur kal?")

        # system + user + assistant + user
        assert len(client.requests[1]["messages"]) >= 4

    def test_senders_are_isolated(self, settings):
        agent, _, client = make_agent([
            completion(content="a"), completion(content="b"),
        ], settings)

        agent.answer("u1", "first")
        agent.answer("u2", "second")

        # system + the one user message
        assert len(client.requests[1]["messages"]) == 2


class TestProviderSelection:
    def test_factory_builds_a_nemotron_agent(self):
        from whatsapp_voice.agent.factory import build_agent

        agent = build_agent(
            AgentSettings(provider="nemotron", nvidia_api_key="nvapi-test")
        )
        assert isinstance(agent, NemotronAgent)

    def test_factory_builds_claude_by_default(self):
        from whatsapp_voice.agent.claude_agent import ClaudeAgent
        from whatsapp_voice.agent.factory import build_agent

        assert isinstance(build_agent(AgentSettings(api_key="sk-test")), ClaudeAgent)

    def test_unknown_provider_lists_the_valid_ones(self):
        from whatsapp_voice.agent.factory import build_agent

        with pytest.raises(ValueError, match="nemotron"):
            build_agent(AgentSettings(provider="gpt5", api_key="x"))

    def test_active_model_follows_the_provider(self):
        assert AgentSettings(provider="claude").active_model == "claude-sonnet-5"
        assert AgentSettings(provider="nemotron").active_model.startswith("nvidia/")

    def test_is_configured_checks_the_selected_providers_key(self):
        """An Anthropic key must not make the Nemotron provider look ready."""
        assert AgentSettings(provider="nemotron", api_key="sk-x").is_configured is False
        assert AgentSettings(provider="nemotron", nvidia_api_key="nv").is_configured is True
        assert AgentSettings(provider="claude", nvidia_api_key="nv").is_configured is False
