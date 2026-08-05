"""Shared test doubles.

Lives in its own module so handler tests across phases use one definition of
"a fake WhatsApp client" rather than each file inventing its own and drifting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from whatsapp_voice.agent.base import Agent, AgentReply
from whatsapp_voice.stt.base import Transcriber
from whatsapp_voice.whatsapp import AudioMessage, TextMessage


def make_opus_bytes(directory: Path, seconds: float = 0.5) -> bytes:
    """A real Opus file, so audio conversion runs for real in tests.

    `seconds` matters for the low-yield check, which compares how much text a
    recording produced against how long it was.
    """
    path = directory / f"seed-{seconds}.opus"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libopus", str(path)],
        check=True,
    )
    return path.read_bytes()


class FakeClient:
    """Stands in for WhatsAppClient: records replies, writes fake media."""

    def __init__(self, media: bytes = b"", fetch_error: Exception | None = None,
                 send_error: Exception | None = None):
        self._media = media
        self._fetch_error = fetch_error
        self._send_error = send_error
        self.sent: list[tuple[str, str]] = []

    def fetch_media(self, media_id: str, dest: Path) -> Path:
        if self._fetch_error:
            raise self._fetch_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._media)
        return dest

    def send_text(self, to: str, body: str) -> str:
        if self._send_error:
            raise self._send_error
        self.sent.append((to, body))
        return "wamid.FAKE"

    @property
    def last_reply(self) -> str:
        assert self.sent, "expected a reply, none was sent"
        return self.sent[-1][1]


class FakeTranscriber(Transcriber):
    name = "fake"

    def __init__(self, text: str = "aaj kitne campaign gaye", error: Exception | None = None,
                 language: str | None = "hi-IN"):
        self._text = text
        self._error = error
        self._language = language
        self.warmed = False
        self.calls = 0

    def warmup(self) -> None:
        self.warmed = True

    def _run(self, audio: Path) -> tuple[str, str | None]:
        self.calls += 1
        if self._error:
            raise self._error
        return self._text, self._language


class FakeAgent(Agent):
    def __init__(self, reply: str = "Aaj 12 campaigns gaye.",
                 error: Exception | None = None,
                 tools: tuple[str, ...] = ("get_campaign_count",)):
        self._reply = reply
        self._error = error
        self._tools = tools
        self.asked: list[tuple[str, str]] = []
        self.languages: list[str | None] = []
        self.warmed = False

    def warmup(self) -> None:
        self.warmed = True

    def answer(self, conversation_id: str, message: str,
               language: str | None = None) -> AgentReply:
        self.asked.append((conversation_id, message))
        self.languages.append(language)
        if self._error:
            raise self._error
        return AgentReply(text=self._reply, tools_used=self._tools,
                          input_tokens=200, output_tokens=40)


def audio_message(**overrides) -> AudioMessage:
    base = dict(message_id="wamid.IN", from_number="919876543210", timestamp="1",
                media_id="MEDIA123", mime_type="audio/ogg; codecs=opus", is_voice_note=True)
    base.update(overrides)
    return AudioMessage(**base)


def text_message(text: str = "aur kal ka?", **overrides) -> TextMessage:
    base = dict(message_id="wamid.T", from_number="919876543210",
                timestamp="1", text=text)
    base.update(overrides)
    return TextMessage(**base)
