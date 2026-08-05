"""Agent loop tests. No network — a fake client returns scripted responses in
the same shape the Anthropic SDK produces.
"""

from types import SimpleNamespace

import pytest

from whatsapp_voice.agent.base import AgentError
from whatsapp_voice.agent.claude_agent import ClaudeAgent
from whatsapp_voice.agent.tools import Tool, ToolRegistry
from whatsapp_voice.config import AgentSettings


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name, arguments, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=arguments, id=block_id)


def response(content, stop_reason="end_turn", in_tokens=100, out_tokens=20):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


class FakeMessages:
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
        self.messages = FakeMessages(responses)


class CountTool(Tool):
    name = "get_campaign_count"
    description = "count"
    input_schema = {"type": "object", "properties": {"on": {"type": "string"}},
                    "required": ["on"], "additionalProperties": False}

    def __init__(self):
        self.calls = []

    def run(self, on):
        self.calls.append(on)
        return f"date={on}\ntotal_campaigns=12"


@pytest.fixture
def settings():
    return AgentSettings(api_key="sk-test", max_tool_turns=4)


def make_agent(responses, settings, tools=None):
    tool = tools if tools is not None else CountTool()
    registry = ToolRegistry([tool] if isinstance(tool, Tool) else tool)
    agent = ClaudeAgent(settings, registry, client=FakeClient(responses))
    return agent, tool


class TestDirectAnswer:
    def test_returns_text_when_no_tools_needed(self, settings):
        agent, _ = make_agent([response([text_block("Namaste! Kya jaanna hai?")])], settings)
        reply = agent.answer("919000000001", "hello")
        assert reply.text == "Namaste! Kya jaanna hai?"
        assert reply.tools_used == ()

    def test_accumulates_token_usage(self, settings):
        agent, _ = make_agent(
            [response([text_block("hi")], in_tokens=150, out_tokens=30)], settings
        )
        reply = agent.answer("n", "hello")
        assert (reply.input_tokens, reply.output_tokens) == (150, 30)
        assert reply.total_tokens == 180

    def test_joins_multiple_text_blocks(self, settings):
        agent, _ = make_agent(
            [response([text_block("First."), text_block("Second.")])], settings
        )
        assert agent.answer("n", "hi").text == "First.\n\nSecond."

    def test_empty_response_gets_a_fallback(self, settings):
        agent, _ = make_agent([response([text_block("   ")])], settings)
        assert agent.answer("n", "hi").text == "I don't have an answer for that."


class TestToolLoop:
    def test_calls_the_tool_then_answers(self, settings):
        agent, tool = make_agent([
            response([tool_use_block("get_campaign_count", {"on": "2026-07-28"})],
                     stop_reason="tool_use"),
            response([text_block("Aaj 12 campaigns gaye.")]),
        ], settings)

        reply = agent.answer("919000000001", "aaj kitne campaign gaye?")

        assert reply.text == "Aaj 12 campaigns gaye."
        assert reply.tools_used == ("get_campaign_count",)
        assert tool.calls == ["2026-07-28"]

    def test_tool_results_go_back_in_one_user_message(self, settings):
        """Splitting results across messages trains the model out of parallel calls."""
        agent, _ = make_agent([
            response([
                tool_use_block("get_campaign_count", {"on": "2026-07-28"}, "t1"),
                tool_use_block("get_campaign_count", {"on": "2026-07-27"}, "t2"),
            ], stop_reason="tool_use"),
            response([text_block("done")]),
        ], settings)

        agent.answer("n", "aaj aur kal?")

        second_request = agent.client.messages.requests[1]
        tool_result_messages = [
            m for m in second_request["messages"]
            if m["role"] == "user" and isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        ]
        assert len(tool_result_messages) == 1
        assert len(tool_result_messages[0]["content"]) == 2

    def test_tool_errors_are_sent_back_flagged(self, settings):
        agent, _ = make_agent([
            response([tool_use_block("no_such_tool", {})], stop_reason="tool_use"),
            response([text_block("I couldn't look that up.")]),
        ], settings)

        agent.answer("n", "something")

        results = [
            block
            for message in agent.client.messages.requests[1]["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert results[0]["is_error"] is True

    def test_stops_at_max_tool_turns(self, settings):
        settings = AgentSettings(api_key="sk-test", max_tool_turns=3)
        looping = [
            response([tool_use_block("get_campaign_count", {"on": "2026-07-28"})],
                     stop_reason="tool_use")
            for _ in range(3)
        ]
        agent, _ = make_agent(looping, settings)

        reply = agent.answer("n", "loop please")

        assert "more steps than I could complete" in reply.text
        assert len(reply.tools_used) == 3


class TestRefusalAndErrors:
    def test_refusal_is_reported_and_history_cleared(self, settings):
        agent, _ = make_agent([response([], stop_reason="refusal")], settings)
        reply = agent.answer("n", "something disallowed")
        assert reply.refused is True
        assert "can't help with that" in reply.text

    def test_api_failure_raises_agent_error(self, settings):
        agent, _ = make_agent([RuntimeError("connection reset")], settings)
        with pytest.raises(AgentError, match="RuntimeError: connection reset"):
            agent.answer("n", "hi")

    def test_missing_api_key_is_rejected_at_construction(self):
        with pytest.raises(AgentError, match="ANTHROPIC_API_KEY"):
            ClaudeAgent(AgentSettings(api_key=None), ToolRegistry())


class TestRequestShape:
    def test_sends_model_effort_system_and_tools(self, settings):
        agent, _ = make_agent([response([text_block("hi")])], settings)
        agent.answer("n", "hello")

        request = agent.client.messages.requests[0]
        assert request["model"] == settings.model
        assert request["output_config"] == {"effort": settings.effort}
        assert "reporting assistant for eko" in request["system"]
        assert [t["name"] for t in request["tools"]] == ["get_campaign_count"]

    def test_omits_tools_when_none_registered(self, settings):
        agent = ClaudeAgent(settings, ToolRegistry(),
                            client=FakeClient([response([text_block("hi")])]))
        agent.answer("n", "hello")
        assert "tools" not in agent.client.messages.requests[0]

    def test_no_sampling_parameters_are_sent(self, settings):
        """Sonnet 5 rejects non-default temperature / top_p / top_k."""
        agent, _ = make_agent([response([text_block("hi")])], settings)
        agent.answer("n", "hello")
        request = agent.client.messages.requests[0]
        assert not {"temperature", "top_p", "top_k"} & set(request)


class TestConversationContinuity:
    def test_follow_up_carries_earlier_turns(self, settings):
        agent, _ = make_agent([
            response([text_block("Aaj 12 gaye.")]),
            response([text_block("Kal 9 gaye.")]),
        ], settings)

        agent.answer("919000000001", "aaj kitne campaign gaye?")
        agent.answer("919000000001", "aur kal?")

        history = agent.client.messages.requests[1]["messages"]
        assert len(history) >= 3, "the follow-up must see the earlier exchange"
        assert history[0]["content"] == "aaj kitne campaign gaye?"

    def test_separate_senders_do_not_share_history(self, settings):
        agent, _ = make_agent([
            response([text_block("a")]),
            response([text_block("b")]),
        ], settings)

        agent.answer("919000000001", "first user")
        agent.answer("919000000002", "second user")

        assert len(agent.client.messages.requests[1]["messages"]) == 1
