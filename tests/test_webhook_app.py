import json

import pytest
from fastapi.testclient import TestClient

from whatsapp_voice.config import Settings, WhatsAppSettings
from whatsapp_voice.whatsapp import create_app
from whatsapp_voice.whatsapp.app import SeenMessages
from whatsapp_voice.whatsapp.handlers import EchoHandler, MessageHandler
from whatsapp_voice.whatsapp.payloads import AudioMessage, TextMessage, UnsupportedMessage
from whatsapp_voice.whatsapp.signature import compute_signature

VERIFY_TOKEN = "my-verify-token"
APP_SECRET = "my-app-secret"


class RecordingHandler(MessageHandler):
    def __init__(self, explode=False):
        self.handled = []
        self.warmed = False
        self._explode = explode

    def warmup(self):
        self.warmed = True

    def handle(self, message):
        self.handled.append(message)
        if self._explode:
            raise RuntimeError("handler blew up")


def build(handler=None, *, app_secret=APP_SECRET, verify_token=VERIFY_TOKEN):
    settings = Settings(whatsapp=WhatsAppSettings(
        access_token="tok", phone_number_id="123",
        verify_token=verify_token, app_secret=app_secret,
    ))
    handler = handler or RecordingHandler()
    return TestClient(create_app(settings, handler)), handler


def envelope(*messages):
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


TEXT = {"id": "wamid.T", "from": "919876543210", "timestamp": "1",
        "type": "text", "text": {"body": "hello"}}


def signed_post(client, payload, secret=APP_SECRET):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = compute_signature(raw, secret)
    return client.post("/webhook", content=raw, headers=headers)


class TestHealth:
    def test_health_ok(self):
        client, _ = build()
        assert client.get("/health").json()["status"] == "ok"

    def test_health_reports_what_is_deployed(self):
        """Answers "what is actually running?" without shell access."""
        client, _ = build()
        body = client.get("/health").json()
        for key in ("handler", "access_control", "signature_verification", "stub_data"):
            assert key in body

    def test_health_leaks_no_secrets(self):
        client, _ = build()
        serialised = str(client.get("/health").json())
        for secret in ("tok", "sk-", "EAA", "919"):
            assert secret not in serialised


class TestVerification:
    def test_echoes_challenge_as_plain_text(self):
        client, _ = build()
        response = client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        })
        assert response.status_code == 200
        # Must be the bare string — JSON quotes would fail Meta's check.
        assert response.text == "1158201444"
        assert response.headers["content-type"].startswith("text/plain")

    def test_rejects_wrong_token(self):
        client, _ = build()
        response = client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x",
        })
        assert response.status_code == 403

    def test_rejects_wrong_mode(self):
        client, _ = build()
        response = client.get("/webhook", params={
            "hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "x",
        })
        assert response.status_code == 403

    def test_errors_when_verify_token_unset(self):
        client, _ = build(verify_token=None)
        response = client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "anything", "hub.challenge": "x",
        })
        assert response.status_code == 500


class TestSignatureEnforcement:
    def test_accepts_correctly_signed_payload(self):
        client, handler = build()
        assert signed_post(client, envelope(TEXT)).status_code == 200
        assert len(handler.handled) == 1

    def test_rejects_unsigned_payload(self):
        client, handler = build()
        assert signed_post(client, envelope(TEXT), secret=None).status_code == 403
        assert handler.handled == []

    def test_rejects_wrong_signature(self):
        client, handler = build()
        assert signed_post(client, envelope(TEXT), secret="wrong").status_code == 403
        assert handler.handled == []

    def test_skips_verification_when_no_app_secret(self):
        # Escape hatch for local testing before the secret is configured.
        client, handler = build(app_secret=None)
        assert signed_post(client, envelope(TEXT), secret=None).status_code == 200
        assert len(handler.handled) == 1


class TestReceiving:
    def test_dispatches_parsed_message(self):
        client, handler = build()
        signed_post(client, envelope(TEXT))
        assert isinstance(handler.handled[0], TextMessage)
        assert handler.handled[0].text == "hello"

    def test_deduplicates_retried_delivery(self):
        # Meta retries on non-200 and delivers at least once — the same id
        # arriving twice must not produce two replies.
        client, handler = build()
        signed_post(client, envelope(TEXT))
        signed_post(client, envelope(TEXT))
        assert len(handler.handled) == 1

    def test_distinct_messages_both_handled(self):
        client, handler = build()
        signed_post(client, envelope(TEXT, {**TEXT, "id": "wamid.T2"}))
        assert len(handler.handled) == 2

    def test_status_callback_returns_200_with_no_work(self):
        client, handler = build()
        payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}
        assert signed_post(client, payload).status_code == 200
        assert handler.handled == []

    def test_invalid_json_returns_200(self):
        # Returning an error would make Meta retry the same bad body forever.
        client, _ = build()
        raw = b"not json at all"
        response = client.post("/webhook", content=raw, headers={
            "X-Hub-Signature-256": compute_signature(raw, APP_SECRET),
        })
        assert response.status_code == 200

    def test_handler_failure_still_returns_200(self):
        client, handler = build(RecordingHandler(explode=True))
        assert signed_post(client, envelope(TEXT)).status_code == 200
        assert len(handler.handled) == 1  # it ran, it raised, the request survived


class TestSeenMessages:
    def test_reports_repeat_ids(self):
        seen = SeenMessages()
        assert seen.seen_before("a") is False
        assert seen.seen_before("a") is True

    def test_ids_expire_by_time_not_by_count(self):
        """Bounding by capacity was the wrong axis: a busy minute could evict an
        id that Meta then retried, producing exactly the duplicate reply this
        prevents. A TTL longer than the retry schedule cannot do that."""
        from whatsapp_voice.store import MemoryStore

        store = MemoryStore()
        seen = SeenMessages(store)
        for index in range(50):
            seen.seen_before(f"id-{index}")

        assert seen.seen_before("id-0") is True, "an old id was forgotten too early"

    def test_the_claim_is_atomic(self):
        """A read-then-write would let two workers both find an id absent and
        both reply — the duplicate that only shows up under load."""
        from whatsapp_voice.store import MemoryStore

        store = MemoryStore()
        first, second = SeenMessages(store), SeenMessages(store)

        assert first.seen_before("wamid.X") is False
        assert second.seen_before("wamid.X") is True

    def test_two_workers_share_one_set_of_ids(self):
        """The whole point: worker B must know what worker A has answered."""
        from whatsapp_voice.store import MemoryStore

        shared = MemoryStore()
        worker_a, worker_b = SeenMessages(shared), SeenMessages(shared)

        worker_a.seen_before("wamid.Y")
        assert worker_b.seen_before("wamid.Y") is True

    def test_separate_stores_do_not_share(self):
        """And the failure this replaces, stated as a test: two processes with
        their own state each answer the same message."""
        from whatsapp_voice.store import MemoryStore

        worker_a = SeenMessages(MemoryStore())
        worker_b = SeenMessages(MemoryStore())

        worker_a.seen_before("wamid.Z")
        assert worker_b.seen_before("wamid.Z") is False


class TestEchoHandler:
    @pytest.fixture
    def sent(self):
        class FakeClient:
            def __init__(self):
                self.messages = []

            def send_text(self, to, body):
                self.messages.append((to, body))
                return "wamid.R"

        return FakeClient()

    def test_echoes_text(self, sent):
        EchoHandler(sent).handle(
            TextMessage(message_id="1", from_number="91987", timestamp="1", text="hi")
        )
        assert sent.messages == [("91987", "Echo: hi")]

    def test_acknowledges_voice_note_without_transcribing(self, sent):
        EchoHandler(sent).handle(AudioMessage(
            message_id="1", from_number="91987", timestamp="1",
            media_id="M", mime_type="audio/ogg", is_voice_note=True,
        ))
        to, body = sent.messages[0]
        assert "voice note" in body
        assert "Phase 3" in body

    def test_explains_unsupported_kind(self, sent):
        EchoHandler(sent).handle(UnsupportedMessage(
            message_id="1", from_number="91987", timestamp="1", kind="image",
        ))
        assert "image" in sent.messages[0][1]


class TestHandlerSelection:
    """`HANDLER` in the environment picks the behaviour, so `uvicorn --reload`
    (which re-imports the factory in a fresh process) keeps the same one."""

    @staticmethod
    def _settings(handler_name):
        return Settings(
            whatsapp=WhatsAppSettings(access_token="tok", phone_number_id="123"),
            handler=handler_name,
        )

    def test_echo_is_the_default(self):
        from whatsapp_voice.whatsapp import EchoHandler as Echo
        from whatsapp_voice.whatsapp.app import build_handler

        assert isinstance(build_handler(self._settings("echo")), Echo)

    def test_transcribe_builds_a_transcribe_handler(self):
        from whatsapp_voice.config import SttSettings
        from whatsapp_voice.whatsapp import TranscribeHandler
        from whatsapp_voice.whatsapp.app import build_handler

        settings = Settings(
            whatsapp=WhatsAppSettings(access_token="tok", phone_number_id="123"),
            stt=SttSettings(whisper_model="tiny"),
            handler="transcribe",
        )
        handler = build_handler(settings)
        assert isinstance(handler, TranscribeHandler)

    def test_unknown_handler_names_the_valid_options(self):
        from whatsapp_voice.whatsapp.app import build_handler

        with pytest.raises(ValueError, match="echo.*transcribe"):
            build_handler(self._settings("nonsense"))


class TestWarmup:
    def test_create_app_warms_the_handler(self):
        _client, handler = build()
        assert handler.warmed is True, "models must load at startup, not on the first message"
