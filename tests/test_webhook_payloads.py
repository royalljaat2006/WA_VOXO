import pytest

from whatsapp_voice.whatsapp.payloads import (
    AudioMessage,
    TextMessage,
    UnsupportedMessage,
    parse_webhook,
)


def envelope(*messages, **extra_value):
    """Wrap raw message dicts in Meta's four-level webhook envelope."""
    value = {"messaging_product": "whatsapp", **extra_value}
    if messages:
        value["messages"] = list(messages)
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


TEXT = {"id": "wamid.T", "from": "919876543210", "timestamp": "1770000000",
        "type": "text", "text": {"body": "aaj kitne campaign gaye?"}}

VOICE = {"id": "wamid.V", "from": "919876543210", "timestamp": "1770000001",
         "type": "audio", "audio": {"id": "MEDIA123", "mime_type": "audio/ogg; codecs=opus",
                                    "voice": True, "sha256": "abc"}}


class TestTextMessages:
    def test_parses_text(self):
        (msg,) = parse_webhook(envelope(TEXT))
        assert isinstance(msg, TextMessage)
        assert msg.text == "aaj kitne campaign gaye?"
        assert msg.from_number == "919876543210"
        assert msg.message_id == "wamid.T"

    def test_text_without_body_is_unsupported(self):
        (msg,) = parse_webhook(envelope({**TEXT, "text": {}}))
        assert isinstance(msg, UnsupportedMessage)


class TestAudioMessages:
    def test_parses_voice_note(self):
        (msg,) = parse_webhook(envelope(VOICE))
        assert isinstance(msg, AudioMessage)
        assert msg.media_id == "MEDIA123"
        assert msg.is_voice_note is True
        assert "opus" in msg.mime_type

    def test_attached_audio_file_is_not_a_voice_note(self):
        raw = {**VOICE, "audio": {"id": "M2", "mime_type": "audio/mpeg"}}
        (msg,) = parse_webhook(envelope(raw))
        assert isinstance(msg, AudioMessage)
        assert msg.is_voice_note is False

    def test_audio_without_media_id_is_unsupported(self):
        (msg,) = parse_webhook(envelope({**VOICE, "audio": {}}))
        assert isinstance(msg, UnsupportedMessage)


class TestOtherKinds:
    def test_image_is_unsupported_but_kept(self):
        raw = {"id": "wamid.I", "from": "91987", "timestamp": "1", "type": "image",
               "image": {"id": "IMG"}}
        (msg,) = parse_webhook(envelope(raw))
        assert isinstance(msg, UnsupportedMessage)
        assert msg.kind == "image"


class TestNonMessageTraffic:
    def test_status_callbacks_yield_nothing(self):
        # Delivery/read receipts for messages WE sent — the most common webhook
        # by volume. Must not be treated as user input.
        payload = envelope(statuses=[{"id": "wamid.X", "status": "delivered"}])
        assert parse_webhook(payload) == []

    def test_empty_payload(self):
        assert parse_webhook({}) == []

    def test_garbage_shapes_do_not_raise(self):
        for bad in [{"entry": "nope"},
                    {"entry": [{"changes": "nope"}]},
                    {"entry": [{"changes": [{"value": None}]}]},
                    {"entry": [{"changes": [{"value": {"messages": "nope"}}]}]},
                    {"entry": [{"changes": [{"value": {"messages": ["str"]}}]}]}]:
            assert parse_webhook(bad) == []

    def test_message_without_id_or_sender_is_skipped(self):
        assert parse_webhook(envelope({"type": "text", "text": {"body": "hi"}})) == []


class TestBatching:
    def test_multiple_messages_in_one_delivery(self):
        messages = parse_webhook(envelope(TEXT, VOICE))
        assert [type(m).__name__ for m in messages] == ["TextMessage", "AudioMessage"]


class TestGroupMessages:
    """`group_id` is the discriminator the whole group path hangs off. Missing it
    makes a group message look like a 1:1, which sends a group answer to one
    person privately."""

    @staticmethod
    def _webhook(message):
        return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}

    def test_a_1to1_message_has_no_group(self):
        [parsed] = parse_webhook(self._webhook({
            "id": "wamid.T", "from": "919876543210", "timestamp": "1",
            "type": "text", "text": {"body": "hello"},
        }))
        assert parsed.group_id is None
        assert parsed.is_group is False
        assert parsed.reply_to == "919876543210"

    @pytest.mark.parametrize("shape", [
        {"group_id": "120363@g.us"},
        {"group": "120363@g.us"},
        {"group": {"id": "120363@g.us"}},
        {"context": {"group_id": "120363@g.us"}},
    ])
    def test_every_known_spelling_is_recognised(self, shape):
        """Meta has moved this field between shapes. Picking one and being
        silently wrong on the others is the failure that leaks a reply."""
        [parsed] = parse_webhook(self._webhook({
            "id": "wamid.T", "from": "919876543210", "timestamp": "1",
            "type": "text", "text": {"body": "hello"}, **shape,
        }))
        assert parsed.group_id == "120363@g.us"
        assert parsed.is_group is True

    def test_replies_go_to_the_group_not_the_sender(self):
        [parsed] = parse_webhook(self._webhook({
            "id": "wamid.T", "from": "919876543210", "timestamp": "1",
            "group_id": "120363@g.us", "type": "text", "text": {"body": "hi"},
        }))
        assert parsed.reply_to == "120363@g.us"
        assert parsed.from_number == "919876543210"

    def test_audio_carries_the_group_too(self):
        [parsed] = parse_webhook(self._webhook({
            "id": "wamid.A", "from": "919876543210", "timestamp": "1",
            "group_id": "120363@g.us", "type": "audio",
            "audio": {"id": "M1", "mime_type": "audio/ogg", "voice": True},
        }))
        assert parsed.group_id == "120363@g.us"
        assert parsed.media_id == "M1"

    def test_a_malformed_group_field_falls_back_to_1to1(self):
        """Better a private reply than a crash — and the handler still works."""
        [parsed] = parse_webhook(self._webhook({
            "id": "wamid.T", "from": "919876543210", "timestamp": "1",
            "group": 12345, "type": "text", "text": {"body": "hi"},
        }))
        assert parsed.group_id is None
