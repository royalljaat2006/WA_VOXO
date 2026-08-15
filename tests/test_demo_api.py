"""Demo API tests.

The security-relevant properties get the most attention here, because this
router is the one thing in the project that answers without a verified phone
number behind it.
"""

import pytest
from fastapi.testclient import TestClient

from whatsapp_voice.agent.base import AgentError
from whatsapp_voice.config import (
    AgentSettings,
    DemoSettings,
    Settings,
    SttSettings,
    WhatsAppSettings,
)
from whatsapp_voice.demo import DemoDeps, create_demo_router

from .fakes import FakeAgent, FakeTranscriber, make_opus_bytes


def build(demo=None, agent=None, transcriber=None):
    from fastapi import FastAPI

    settings = Settings(
        whatsapp=WhatsAppSettings(access_token="tok", phone_number_id="123"),
        stt=SttSettings(whisper_model="tiny"),
        agent=AgentSettings(api_key="sk-test"),
        demo=demo or DemoSettings(enabled=True),
        handler="agent",
    )
    app = FastAPI()
    app.include_router(
        create_demo_router(
            DemoDeps(settings=settings, transcriber=transcriber, agent=agent)
        )
    )
    return TestClient(app), settings


class TestGating:
    def test_disabled_demo_returns_404_not_403(self):
        """404 rather than 403: a disabled demo should not advertise that it exists."""
        client, _ = build(demo=DemoSettings(enabled=False))
        assert client.get("/demo/status").status_code == 404
        assert client.post("/demo/ask", json={"question": "hi"}).status_code == 404

    def test_token_required_when_set(self):
        client, _ = build(demo=DemoSettings(enabled=True, token="s3cret"))
        assert client.get("/demo/status").status_code == 401

    def test_correct_token_is_accepted(self):
        client, _ = build(demo=DemoSettings(enabled=True, token="s3cret"))
        response = client.get("/demo/status", headers={"X-Demo-Token": "s3cret"})
        assert response.status_code == 200

    def test_wrong_token_is_rejected(self):
        client, _ = build(demo=DemoSettings(enabled=True, token="s3cret"))
        response = client.get("/demo/status", headers={"X-Demo-Token": "guess"})
        assert response.status_code == 401

    def test_disabled_by_default(self):
        assert DemoSettings().enabled is False


class TestStatus:
    def test_reports_configuration(self):
        client, _ = build(agent=FakeAgent(), transcriber=FakeTranscriber())
        body = client.get("/demo/status").json()
        assert body["handler"] == "agent"
        assert body["speech_engine"] == "fake"
        assert body["stub_data"] is True

    def test_says_when_pieces_are_missing(self):
        client, _ = build()
        body = client.get("/demo/status").json()
        assert body["speech_engine"] == "not configured"
        assert body["model"] == "not configured"

    def test_leaks_no_secrets(self):
        client, _ = build(agent=FakeAgent(), transcriber=FakeTranscriber())
        serialised = str(client.get("/demo/status").json())
        for secret in ("sk-test", "tok", "919876543210"):
            assert secret not in serialised


class TestAsk:
    def test_returns_the_agents_answer(self):
        client, _ = build(agent=FakeAgent("Aaj 9 campaigns gaye."))
        body = client.post("/demo/ask", json={"question": "aaj kitne?"}).json()
        assert body["answer"] == "Aaj 9 campaigns gaye."
        assert body["tools_used"] == ["get_campaign_count"]

    def test_reports_timing_and_tokens(self):
        client, _ = build(agent=FakeAgent())
        body = client.post("/demo/ask", json={"question": "hi"}).json()
        assert [s["name"] for s in body["stages"]] == ["agent"]
        assert body["input_tokens"] == 200

    def test_session_id_scopes_history(self):
        agent = FakeAgent()
        client, _ = build(agent=agent)
        client.post("/demo/ask", json={"question": "one", "session_id": "a"})
        client.post("/demo/ask", json={"question": "two", "session_id": "b"})
        assert [conversation for conversation, _ in agent.asked] == ["a", "b"]

    def test_missing_agent_is_explained(self):
        client, _ = build(agent=None)
        response = client.post("/demo/ask", json={"question": "hi"})
        assert response.status_code == 503
        assert "ANTHROPIC_API_KEY" in response.json()["detail"]

    def test_agent_failure_is_explained(self):
        client, _ = build(agent=FakeAgent(error=AgentError("upstream down")))
        response = client.post("/demo/ask", json={"question": "hi"})
        assert response.status_code == 502
        assert "upstream down" in response.json()["detail"]

    def test_empty_question_rejected(self):
        client, _ = build(agent=FakeAgent())
        assert client.post("/demo/ask", json={"question": ""}).status_code == 422


class TestVoice:
    @pytest.fixture
    def audio(self, tmp_path):
        return make_opus_bytes(tmp_path)

    def test_transcribes_then_answers(self, audio):
        client, _ = build(
            agent=FakeAgent("Aaj 9 gaye."),
            transcriber=FakeTranscriber("aaj kitne campaign gaye"),
        )
        body = client.post(
            "/demo/voice",
            files={"audio": ("recording.opus", audio, "audio/ogg")},
        ).json()

        assert body["transcript"] == "aaj kitne campaign gaye"
        assert body["answer"] == "Aaj 9 gaye."

    def test_reports_every_stage_in_order(self, audio):
        client, _ = build(agent=FakeAgent(), transcriber=FakeTranscriber())
        body = client.post(
            "/demo/voice", files={"audio": ("r.opus", audio, "audio/ogg")}
        ).json()
        assert [s["name"] for s in body["stages"]] == [
            "upload", "convert", "transcribe", "agent",
        ]

    def test_transcript_only_when_no_agent(self, audio):
        """Without an agent this still demonstrates Phases 0 and 3."""
        client, _ = build(agent=None, transcriber=FakeTranscriber("hello there"))
        body = client.post(
            "/demo/voice", files={"audio": ("r.opus", audio, "audio/ogg")}
        ).json()
        assert body["transcript"] == "hello there"
        assert "hello there" in body["answer"]

    def test_empty_transcript_says_so(self, audio):
        client, _ = build(agent=FakeAgent(), transcriber=FakeTranscriber(text="  "))
        body = client.post(
            "/demo/voice", files={"audio": ("r.opus", audio, "audio/ogg")}
        ).json()
        assert "didn't hear anything" in body["answer"]

    def test_unconvertible_audio_is_explained(self):
        client, _ = build(agent=FakeAgent(), transcriber=FakeTranscriber())
        response = client.post(
            "/demo/voice", files={"audio": ("r.opus", b"not audio", "audio/ogg")}
        )
        assert response.status_code == 422
        assert "could not be converted" in response.json()["detail"]

    def test_missing_transcriber_is_explained(self, audio):
        client, _ = build(transcriber=None)
        response = client.post(
            "/demo/voice", files={"audio": ("r.opus", audio, "audio/ogg")}
        )
        assert response.status_code == 503

    def test_agent_failure_still_reports_what_was_heard(self, audio):
        """Losing the transcript on an agent error would hide the useful half."""
        client, _ = build(
            agent=FakeAgent(error=AgentError("model down")),
            transcriber=FakeTranscriber("aaj kitne campaign gaye"),
        )
        response = client.post(
            "/demo/voice", files={"audio": ("r.opus", audio, "audio/ogg")}
        )
        assert response.status_code == 502
        assert "aaj kitne campaign gaye" in response.json()["detail"]
