"""What to do with a message once it has been received and parsed.

`MessageHandler` is the seam between the transport (the webhook server, which
never changes again) and the product logic (which changes every phase):

    Phase 2  EchoHandler          — proves the round trip works
    Phase 3  TranscribeHandler    — voice note in, transcript out
    Phase 4  AgentHandler         — transcript → internal API → answer

Swapping phases means passing a different handler to `create_app`, not editing
the server.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from .. import audio as audio_utils
from ..agent import Agent, AgentError
from ..stt import Transcriber, TranscriptionError
from ..stt.base import Transcript, is_low_yield
from .client import WhatsAppClient, WhatsAppError
from .payloads import AudioMessage, IncomingMessage, TextMessage, UnsupportedMessage

logger = logging.getLogger(__name__)


class MessageHandler(ABC):
    def warmup(self) -> None:  # noqa: B027 - intentionally optional, not abstract
        """Do expensive one-time setup before the first message arrives.

        Called once at server start. Without it, loading a speech model happens
        while a real user is waiting, adding tens of seconds to the first reply.
        Default is a no-op; must be idempotent.
        """

    @abstractmethod
    def handle(self, message: IncomingMessage) -> None:
        """Act on one message. Runs in the background, after the webhook 200s.

        Exceptions are caught and logged by the caller — a failure here must not
        take down the server or trigger a Meta retry.
        """


class EchoHandler(MessageHandler):
    """Phase 2 acceptance: reply with what we understood.

    Deliberately does not transcribe audio — that is Phase 3. For a voice note
    it confirms receipt so the round trip is visible end to end.
    """

    def __init__(self, client: WhatsAppClient) -> None:
        self._client = client

    def handle(self, message: IncomingMessage) -> None:
        if message.is_group:
            self._client.send_group_text(message.reply_to, self._reply_for(message))
        else:
            self._client.send_text(message.reply_to, self._reply_for(message))

    @staticmethod
    def _reply_for(message: IncomingMessage) -> str:
        if isinstance(message, TextMessage):
            return f"Echo: {message.text}"

        if isinstance(message, AudioMessage):
            kind = "voice note" if message.is_voice_note else "audio file"
            return (
                f"Got your {kind} ({message.mime_type or 'unknown format'}). "
                "Transcription is coming in Phase 3."
            )

        kind = message.kind if isinstance(message, UnsupportedMessage) else "message"
        return f"Sorry, I can't handle a {kind} yet. Send text or a voice note."


class TranscribeHandler(MessageHandler):
    """Phase 3 acceptance: voice note in, transcript out.

    The pipeline is four existing pieces wired together — nothing here is new
    machinery:

        client.fetch_media()  →  audio.to_wav()  →  transcriber.transcribe()
                                                 →  client.send_text()

    Phase 4 replaces the last step: instead of echoing the transcript, the
    transcript goes to the agent and the agent's answer is sent.

    Every failure path replies with something. Silence is the worst outcome for
    the user — they cannot tell a broken bot from a slow one.
    """

    def __init__(self, client: WhatsAppClient, transcriber: Transcriber) -> None:
        self._client = client
        self._transcriber = transcriber

    def warmup(self) -> None:
        logger.info("Loading speech engine %s …", self._transcriber.name)
        self._transcriber.warmup()
        logger.info("Speech engine ready")

    def handle(self, message: IncomingMessage) -> None:
        if isinstance(message, AudioMessage):
            self._handle_audio(message)
            return

        if isinstance(message, TextMessage):
            self._reply(
                message,
                "I'm listening for voice notes — hold the mic button and speak. "
                f"(You sent text: “{message.text}”)",
            )
            return

        kind = message.kind if isinstance(message, UnsupportedMessage) else "message"
        self._reply(message, f"Sorry, I can't handle a {kind} yet. Send a voice note instead.")

    # ----------------------------------------------------------------------

    def _handle_audio(self, message: AudioMessage) -> None:
        # Downloaded audio is customer data — keep it off disk beyond the work
        # that needs it, rather than accumulating voice notes in the repo.
        workdir = Path(tempfile.mkdtemp(prefix="voice-"))
        duration_s = 0.0
        try:
            result, duration_s = self._transcribe(message, workdir)
        except WhatsAppError as exc:
            logger.warning("Could not fetch media %s: %s", message.media_id, exc)
            self._reply(message, "I couldn't download that voice note. Please send it again.")
            return
        except TranscriptionError as exc:
            logger.warning("Transcription failed for %s: %s", message.message_id, exc)
            self._reply(message, "I couldn't make out that recording. Please try again.")
            return
        except audio_utils.AudioError as exc:
            logger.warning("Audio conversion failed for %s: %s", message.message_id, exc)
            self._reply(message, "That audio format didn't work. Please record a voice note.")
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if not result.text:
            self._reply(message, "I didn't hear anything in that voice note. Try again?")
            return

        # A recording that produced far less text than its length implies is a
        # failed recognition wearing the clothes of a short instruction. Acting
        # on the fragment is the expensive mistake; asking again is not.
        if self._is_low_yield(message, result.text, duration_s):
            logger.info(
                "Low-yield transcript for %s: %d chars from %.1fs",
                message.message_id, len(result.text), duration_s,
            )
            self._reply(message, self._low_yield_reply(message, result.text))
            return

        self._on_transcript(message, result.text, result.language)

    def _is_low_yield(self, message: AudioMessage, text: str, duration_s: float) -> bool:
        """Whether this transcript should be treated as a failed recognition.

        Overridable: `is_low_yield`'s char-per-second ratio is only exempted
        below `MIN_DURATION_FOR_YIELD_CHECK_S` (2.5s), but a PIN's *content*
        is always tiny ("1234" is 4 characters) while speaking one carefully
        commonly runs well past 2.5s — pausing to recall it, reading it back
        digit by digit. Once duration crosses ~1.3s, 4 chars / duration is
        already under the 3.0 chars/s threshold, so a correctly-recognised
        PIN gets discarded as noise purely for being short content in a
        longer recording — not for being wrong. `PaymentHandler` overrides
        this to trust a transcript that already parses as a valid PIN over
        the density heuristic.
        """
        return is_low_yield(text, duration_s)

    def _low_yield_reply(self, message: AudioMessage, text: str) -> str:
        """The reply when a recording produced far less text than its length
        implies. Overridable: this runs before `_on_transcript`, so a
        payment-aware subclass has not yet had a chance to decide whether
        `message` might be a PIN — `PaymentHandler` overrides this to
        withhold `text` while a PIN could be mid-flight, the same reasoning
        as `_log_transcript`, applied to an outgoing reply instead of a log
        line.
        """
        return f"Awaaz saaf nahi aayi — sirf itna suna: “{text}”. Thoda dobara boliye?"

    def _on_transcript(
        self, message: AudioMessage, transcript: str, language: str | None = None
    ) -> None:
        """What to do once audio has become text.

        The extension point for Phase 4: this handler sends the transcript back,
        while AgentHandler sends it to the agent. Splitting it here means the
        download / convert / transcribe / cleanup logic above is written once.
        """
        self._reply(message, f"🎤 {transcript}")

    def _transcribe(self, message: AudioMessage, workdir: Path) -> tuple[Transcript, float]:
        """Returns the whole Transcript plus the audio's length in seconds.

        The detected language rides along with the words it came from, rather
        than being stashed on the handler — which would cross wires between two
        callers being served at once, since handlers are shared across
        background tasks. The duration travels the same way, for the same
        reason; the caller needs it to judge whether the transcript is credible.
        """
        source = self._client.fetch_media(message.media_id, workdir / message.media_id)
        wav = audio_utils.to_wav(source, workdir)
        result = self._transcriber.transcribe(wav)
        self._log_transcript(message, result)

        try:
            duration_s = audio_utils.duration_seconds(wav)
        except audio_utils.AudioError:
            # Not worth failing a good transcript over — the yield check simply
            # does not run.
            duration_s = 0.0

        return result, duration_s

    def _log_transcript(self, message: AudioMessage, result: Transcript) -> None:
        """Record what was heard.

        Overridable because a transcript is not always safe to log: when a
        payment is awaiting confirmation the next voice note may be nothing but
        a PIN. `PaymentHandler` suppresses it in exactly that window.
        """
        logger.info(
            "Transcribed %s in %.1fs [%s]: %s",
            message.message_id, result.latency_s, result.language or "?", result.text[:120],
        )

    def _reply(self, message: IncomingMessage, text: str) -> None:
        """Send a reply back where the message came from.

        `reply_to`, never `from_number`: in a group those differ, and answering
        the sender would turn a group answer into a surprise private message.
        """
        try:
            if message.is_group:
                self._client.send_group_text(message.reply_to, text)
            else:
                self._client.send_text(message.reply_to, text)
        except WhatsAppError:
            # Nothing left to try — the user gets silence, so make sure the
            # operator does not.
            logger.exception("Failed to reply to %s", message.reply_to)


class AgentHandler(TranscribeHandler):
    """Phase 4 acceptance: a spoken question gets a real answer.

    Extends TranscribeHandler because the front half of the pipeline is
    identical — fetch, convert, transcribe. Only the last step differs: the
    transcript goes to the agent instead of straight back to the user.

    Text messages are answered too. Voice is the point of the product, but a
    typed follow-up ("and yesterday?") should not be rejected on principle.
    """

    def __init__(
        self,
        client: WhatsAppClient,
        transcriber: Transcriber,
        agent: Agent,
        echo_transcript: bool = True,
    ) -> None:
        super().__init__(client, transcriber)
        self._agent = agent
        # Showing what we heard makes ASR errors debuggable for the user — they
        # can see "delivery report" became "delivery force" and rephrase, rather
        # than wondering why the answer is wrong. Worth two lines in the reply.
        self._echo_transcript = echo_transcript

    def warmup(self) -> None:
        super().warmup()
        self._agent.warmup()

    def handle(self, message: IncomingMessage) -> None:
        if isinstance(message, AudioMessage):
            self._handle_audio(message)
            return

        if isinstance(message, TextMessage):
            self._answer(message, message.text)
            return

        kind = message.kind if isinstance(message, UnsupportedMessage) else "message"
        self._reply(
            message,
            f"Sorry, I can't handle a {kind} yet. Send a voice note or type your question.",
        )

    def _on_transcript(
        self, message: AudioMessage, transcript: str, language: str | None = None
    ) -> None:
        """Called by TranscribeHandler once a voice note has become text."""
        prefix = f"🎤 “{transcript}”\n\n" if self._echo_transcript else ""
        self._answer(message, transcript, prefix=prefix, language=language)

    def _answer(
        self,
        message: IncomingMessage,
        question: str,
        prefix: str = "",
        language: str | None = None,
    ) -> None:
        # Scoped to the conversation, not the speaker: in a group, "aur kal ka?"
        # from a second person is a follow-up to the same thread.
        try:
            reply = self._agent.answer(message.reply_to, question, language=language)
        except AgentError as exc:
            logger.warning("Agent failed for %s: %s", message.from_number, exc)
            self._reply(
                message,
                f"{prefix}I couldn't work that out just now. Please try again in a moment.",
            )
            return

        logger.info(
            "Answered %s — tools=%s tokens=%d/%d%s",
            message.from_number, list(reply.tools_used) or "none",
            reply.input_tokens, reply.output_tokens,
            " [refused]" if reply.refused else "",
        )
        self._reply(message, f"{prefix}{reply.text}")
