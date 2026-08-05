"""Replying in the language the user actually spoke.

Sarvam detects the language and returns a BCP-47 tag; that tag has to survive all
the way to the system prompt, or a Tamil speaker gets a Hinglish answer. The tag
goes into the *prompt*, not the message — a hint inside the message text would be
re-read as a user instruction on every later turn.
"""

import pytest

from whatsapp_voice.agent.claude_agent import language_name, system_prompt_for
from whatsapp_voice.config import SttSettings


class TestLanguageName:
    @pytest.mark.parametrize("tag,name", [
        ("hi-IN", "Hindi"),
        ("ta-IN", "Tamil"),
        ("bn-IN", "Bengali"),
        ("mr-IN", "Marathi"),
        ("en-IN", "English"),
        ("te", "Telugu"),
        ("PA-IN", "Punjabi"),
    ])
    def test_names_the_language(self, tag, name):
        assert language_name(tag) == name

    @pytest.mark.parametrize("tag", [None, "", "unknown", "zz-ZZ", "xx"])
    def test_unrecognised_tags_name_nothing(self, tag):
        """Better to say nothing about language than to name the wrong one."""
        assert language_name(tag) is None


class TestSystemPrompt:
    def test_always_carries_todays_date(self):
        from datetime import date
        assert date.today().isoformat() in system_prompt_for()

    def test_names_the_language_when_known(self):
        prompt = system_prompt_for(language="ta-IN")
        assert "Tamil" in prompt

    def test_adds_nothing_when_the_language_is_unknown(self):
        """Sarvam returns the literal "unknown" when detection fails. The base
        prompt already tells the model to mirror the user's language; naming a
        language it did not detect would be worse than saying nothing."""
        assert system_prompt_for(language="unknown") == system_prompt_for()

    def test_no_language_is_the_same_as_an_unknown_one(self):
        assert system_prompt_for(language=None) == system_prompt_for(language="zz")

    def test_tag_itself_is_never_shown_to_the_model(self):
        """A model told "reply in ta-IN" is guessing; one told "Tamil" is not."""
        assert "ta-IN" not in system_prompt_for(language="ta-IN")


class TestSarvamDefault:
    def test_defaults_to_auto_detect(self, monkeypatch):
        """The default has to be auto-detect, or every non-Hindi speaker is
        silently mistranscribed."""
        monkeypatch.delenv("SARVAM_LANG", raising=False)
        assert SttSettings.from_env().sarvam_language == "unknown"

    def test_can_still_be_pinned(self, monkeypatch):
        monkeypatch.setenv("SARVAM_LANG", "hi-IN")
        assert SttSettings.from_env().sarvam_language == "hi-IN"


class TestAgentsAcceptLanguage:
    def test_claude_agent_threads_it_into_the_request(self):
        from whatsapp_voice.agent.claude_agent import ClaudeAgent
        from whatsapp_voice.agent.tools import ToolRegistry
        from whatsapp_voice.config import AgentSettings

        captured = {}

        class FakeMessages:
            def create(self, **request):
                captured.update(request)
                return _reply("Vanakkam.")

        class FakeClient:
            messages = FakeMessages()

        agent = ClaudeAgent(AgentSettings(api_key="k"), ToolRegistry(), client=FakeClient())
        agent.answer("919876543210", "enna nadakkuthu", language="ta-IN")

        assert "Tamil" in captured["system"]


def _reply(text):
    """Minimal stand-in for an Anthropic response object."""
    class Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class Usage:
        input_tokens = 10
        output_tokens = 5

    class Response:
        content = [Block(text)]
        usage = Usage()
        stop_reason = "end_turn"

    return Response()
