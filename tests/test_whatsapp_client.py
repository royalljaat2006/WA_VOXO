import json
from pathlib import Path

import pytest
import requests

from whatsapp_voice.config import WhatsAppSettings
from whatsapp_voice.whatsapp import WhatsAppClient, WhatsAppError


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_body
        self._content = content
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=8192):
        yield self._content


class RecordingSession:
    """Stand-in for requests.Session that returns queued responses and records calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.calls = []

    def _next(self):
        if not self._responses:
            raise AssertionError("more HTTP calls than queued responses")
        return self._responses.pop(0)

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return self._next()

    def get(self, url, timeout=None, stream=False):
        self.calls.append(("GET", url, None))
        return self._next()


def make_settings(**overrides):
    base = dict(access_token="tok", phone_number_id="123", api_version="v21.0")
    base.update(overrides)
    return WhatsAppSettings(**base)


class TestConstruction:
    def test_requires_configuration(self):
        with pytest.raises(WhatsAppError, match="not configured"):
            WhatsAppClient(WhatsAppSettings())

    def test_sets_bearer_header(self):
        session = RecordingSession([])
        WhatsAppClient(make_settings(access_token="secret"), session=session)
        assert session.headers["Authorization"] == "Bearer secret"


class TestSendText:
    def test_posts_to_messages_endpoint_and_returns_wamid(self):
        session = RecordingSession([
            FakeResponse(json_body={"messages": [{"id": "wamid.ABC"}]}),
        ])
        client = WhatsAppClient(make_settings(), session=session)

        wamid = client.send_text("919876543210", "hi")

        assert wamid == "wamid.ABC"
        method, url, body = session.calls[0]
        assert method == "POST"
        assert url == "https://graph.facebook.com/v21.0/123/messages"
        assert body["to"] == "919876543210"
        assert body["text"]["body"] == "hi"
        assert body["messaging_product"] == "whatsapp"

    def test_surfaces_meta_error_message(self):
        session = RecordingSession([
            FakeResponse(status_code=401, json_body={
                "error": {"message": "Session expired", "code": 190}
            }),
        ])
        client = WhatsAppClient(make_settings(), session=session)

        with pytest.raises(WhatsAppError) as exc:
            client.send_text("919876543210", "hi")
        assert exc.value.status_code == 401
        assert "Session expired" in str(exc.value)
        assert "190" in str(exc.value)

    def test_unexpected_shape_raises(self):
        session = RecordingSession([FakeResponse(json_body={"messages": []})])
        client = WhatsAppClient(make_settings(), session=session)
        with pytest.raises(WhatsAppError, match="Unexpected send response"):
            client.send_text("919876543210", "hi")


class TestMedia:
    def test_get_media_url_returns_url(self):
        session = RecordingSession([
            FakeResponse(json_body={"url": "https://lookaside.fbsbx.com/xyz"}),
        ])
        client = WhatsAppClient(make_settings(), session=session)
        assert client.get_media_url("MID").endswith("/xyz")
        assert session.calls[0][1] == "https://graph.facebook.com/v21.0/MID"

    def test_get_media_url_missing_url_raises(self):
        session = RecordingSession([FakeResponse(json_body={"id": "MID"})])
        client = WhatsAppClient(make_settings(), session=session)
        with pytest.raises(WhatsAppError, match="No 'url'"):
            client.get_media_url("MID")

    def test_fetch_media_resolves_then_downloads(self, tmp_path: Path):
        session = RecordingSession([
            FakeResponse(json_body={"url": "https://lookaside.fbsbx.com/voice"}),
            FakeResponse(content=b"OggS-fake-audio"),
        ])
        client = WhatsAppClient(make_settings(), session=session)

        dest = client.fetch_media("MID", tmp_path / "note.ogg")

        assert dest.read_bytes() == b"OggS-fake-audio"
        # Two calls in order: resolve metadata, then download the bytes.
        assert [c[0] for c in session.calls] == ["GET", "GET"]
        assert session.calls[0][1] == "https://graph.facebook.com/v21.0/MID"
        assert session.calls[1][1] == "https://lookaside.fbsbx.com/voice"

    def test_download_failure_raises(self, tmp_path: Path):
        session = RecordingSession([
            FakeResponse(json_body={"url": "https://lookaside.fbsbx.com/voice"}),
            FakeResponse(status_code=404, text="not found"),
        ])
        client = WhatsAppClient(make_settings(), session=session)
        with pytest.raises(WhatsAppError, match="HTTP 404"):
            client.fetch_media("MID", tmp_path / "note.ogg")


def test_real_session_is_used_by_default(monkeypatch):
    # Guard: constructing without an injected session should build a real
    # requests.Session (not error), so production code path is exercised.
    created = {}
    real_session_cls = requests.Session

    def spy():
        session = real_session_cls()
        created["made"] = True
        return session

    monkeypatch.setattr(requests, "Session", spy)
    WhatsAppClient(make_settings())
    assert created.get("made") is True
