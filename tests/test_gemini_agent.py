"""Gemini agent tests.

The tool loop itself is `OpenAICompatibleAgent`'s and is covered in depth by
`test_nemotron_agent.py` — repeating it here would test the same code twice.
What is Gemini-specific, and what this file pins, is the wiring: the right key,
the right endpoint, the right model, and the fact that none of the three leak
across from the NVIDIA settings sitting in the same dataclass.
"""

from types import SimpleNamespace

import pytest

from whatsapp_voice.agent.base import AgentError
from whatsapp_voice.agent.factory import build_agent
from whatsapp_voice.agent.gemini_agent import GeminiAgent
from whatsapp_voice.agent.nemotron_agent import NemotronAgent
from whatsapp_voice.agent.tools import Tool, ToolRegistry
from whatsapp_voice.config import AgentSettings

from .test_nemotron_agent import FakeClient, completion, function_call


class CountTool(Tool):
    name = "get_campaign_count"
    description = "count campaigns on a date"
    input_schema = {
        "type": "object",
        "properties": {"on": {"type": "string"}},
        "required": ["on"],
        "additionalProperties": False,
    }

    def run(self, on):
        return f"date={on}\ntotal_campaigns=12"


@pytest.fixture
def settings():
    return AgentSettings(provider="gemini", gemini_api_key="AIza-test")


def make_agent(responses, settings):
    client = FakeClient(responses)
    agent = GeminiAgent(settings, ToolRegistry([CountTool()]), client=client)
    return agent, client


class TestWiring:
    def test_sends_the_gemini_model(self, settings):
        agent, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert client.requests[0]["model"] == settings.gemini_model

    def test_does_not_send_the_nemotron_model(self, settings):
        """Both live on the same settings object, one field apart."""
        settings = AgentSettings(
            provider="gemini", gemini_api_key="AIza-test",
            gemini_model="gemini-3.6-flash",
            nemotron_model="nvidia/some-other-model",
        )
        agent, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert client.requests[0]["model"] == "gemini-3.6-flash"

    def test_reads_the_gemini_key_not_the_nvidia_one(self):
        assert GeminiAgent.api_key_from(
            AgentSettings(gemini_api_key="AIza-x", nvidia_api_key="nvapi-y")
        ) == "AIza-x"

    def test_points_at_googles_openai_dialect_endpoint(self, settings):
        assert GeminiAgent.base_url_from(settings) == (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        )

    def test_base_url_keeps_its_trailing_slash(self, settings):
        """The SDK joins paths onto it; without the slash the last segment is
        replaced and every request 404s."""
        assert GeminiAgent.base_url_from(settings).endswith("/")

    def test_missing_key_names_the_variable_to_set(self):
        with pytest.raises(AgentError, match="GEMINI_API_KEY"):
            GeminiAgent(AgentSettings(provider="gemini"), ToolRegistry())

    def test_an_nvidia_key_does_not_satisfy_gemini(self):
        """Fail loudly rather than starting up and 401ing on the first real
        message."""
        with pytest.raises(AgentError, match="GEMINI_API_KEY"):
            GeminiAgent(
                AgentSettings(provider="gemini", nvidia_api_key="nvapi-test"),
                ToolRegistry(),
            )


class TestSharesTheToolLoop:
    """Enough to prove the inheritance is live — the depth is in
    test_nemotron_agent.py."""

    def test_runs_a_tool_and_answers(self, settings):
        agent, _ = make_agent(
            [
                completion(tool_calls=[function_call("get_campaign_count", '{"on": "kal"}')]),
                completion(content="Kal 12 campaigns."),
            ],
            settings,
        )
        reply = agent.answer("u1", "kal kitne campaigns?")
        assert reply.text == "Kal 12 campaigns."
        assert reply.tools_used == ("get_campaign_count",)

    def test_tool_results_go_back_one_message_per_call(self, settings):
        """OpenAI's shape, not Anthropic's — inherited, but load-bearing enough
        to assert on this provider too."""
        agent, client = make_agent(
            [
                completion(tool_calls=[function_call("get_campaign_count", '{"on": "kal"}')]),
                completion(content="done"),
            ],
            settings,
        )
        agent.answer("u1", "kal?")

        tool_messages = [
            m for m in client.requests[1]["messages"] if m.get("role") == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call-1"

    def test_tools_are_sent_in_the_nested_function_shape(self, settings):
        agent, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")

        [definition] = client.requests[0]["tools"]
        assert definition["type"] == "function"
        assert definition["function"]["name"] == "get_campaign_count"


class TestSettings:
    def test_active_model_reports_gemini(self):
        settings = AgentSettings(provider="gemini", gemini_model="gemini-3.6-flash")
        assert settings.active_model == "gemini-3.6-flash"

    def test_is_configured_reads_the_gemini_key(self):
        assert AgentSettings(provider="gemini", gemini_api_key="AIza-x").is_configured
        assert not AgentSettings(provider="gemini").is_configured

    def test_another_providers_key_does_not_count_as_configured(self):
        """A GEMINI_API_KEY left over from a trial must not make an
        unconfigured Claude deployment look ready."""
        assert not AgentSettings(
            provider="claude", gemini_api_key="AIza-x"
        ).is_configured
        assert not AgentSettings(
            provider="gemini", api_key="sk-ant-x", nvidia_api_key="nvapi-y"
        ).is_configured


class TestFactory:
    def test_builds_a_gemini_agent(self):
        agent = build_agent(AgentSettings(provider="gemini", gemini_api_key="AIza-x"))
        assert isinstance(agent, GeminiAgent)

    def test_gemini_is_not_nemotron(self):
        """Both inherit the same loop, so `isinstance` on the base would pass
        for either — assert the concrete class."""
        agent = build_agent(AgentSettings(provider="gemini", gemini_api_key="AIza-x"))
        assert not isinstance(agent, NemotronAgent)

    def test_unknown_provider_lists_gemini_as_valid(self):
        with pytest.raises(ValueError, match="gemini"):
            build_agent(AgentSettings(provider="llama"))


class TestFreeTierWarning:
    def test_warns_when_paired_with_a_real_payment_gateway(self, caplog):
        """The free tier's terms let Google read this traffic, and with a real
        gateway that traffic is payee numbers, UPI IDs and amounts."""
        from whatsapp_voice.agent.payments import PendingIntents, StubPaymentGateway
        from whatsapp_voice.config import PaymentSettings

        payments = PaymentSettings(enabled=True, gateway="eko")
        assert payments.moves_real_money

        with caplog.at_level("WARNING"):
            build_agent(
                AgentSettings(provider="gemini", gemini_api_key="AIza-x"),
                payments, StubPaymentGateway(), PendingIntents(),
            )

        assert "human reviewers" in caplog.text
        assert "ai.google.dev/gemini-api/terms" in caplog.text

    def test_does_not_warn_without_real_money(self, caplog):
        with caplog.at_level("WARNING"):
            build_agent(AgentSettings(provider="gemini", gemini_api_key="AIza-x"))
        assert "human reviewers" not in caplog.text


class TestFakeClientContract:
    def test_the_fake_matches_the_sdk_call_shape(self, settings):
        """Guards the shared fake: if the agent stopped calling
        `chat.completions.create`, every test above would still pass."""
        agent, client = make_agent([completion(content="hi")], settings)
        agent.answer("u1", "hello")
        assert isinstance(client.chat.completions.requests[0], dict)
        assert client.requests[0]["messages"][0]["role"] == "system"


def test_nemotron_still_reads_its_own_settings():
    """The refactor that created the shared base must not have crossed the
    wires in the other direction."""
    settings = AgentSettings(
        nvidia_api_key="nvapi-y", gemini_api_key="AIza-x",
        nemotron_model="nvidia/m", gemini_model="gemini-m",
    )
    assert NemotronAgent.api_key_from(settings) == "nvapi-y"
    assert NemotronAgent.model_from(settings) == "nvidia/m"
    assert "nvidia.com" in NemotronAgent.base_url_from(settings)


def test_fake_client_helper_is_importable():
    assert isinstance(FakeClient([]).requests, list)
    assert isinstance(SimpleNamespace(), SimpleNamespace)
