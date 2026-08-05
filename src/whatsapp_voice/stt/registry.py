"""Build the set of engines that current configuration can actually run.

Engines whose credentials are absent are skipped rather than erroring, so the
project works with just the local model on day one and gains engines as keys
are added.
"""

from __future__ import annotations

from ..config import SttSettings
from .base import Transcriber
from .elevenlabs import ElevenLabsTranscriber
from .local_whisper import LocalWhisperTranscriber
from .openai_stt import OpenAITranscriber
from .sarvam import SarvamTranscriber


def build_transcribers(settings: SttSettings) -> list[Transcriber]:
    """All engines the configuration enables, in a stable order."""
    engines: list[Transcriber] = []

    if settings.run_local_whisper:
        engines.append(
            LocalWhisperTranscriber(
                model_size=settings.whisper_model,
                language=settings.whisper_language,
            )
        )

    if settings.openai_api_key:
        engines.extend(
            OpenAITranscriber(
                api_key=settings.openai_api_key,
                model=model,
                timeout_s=settings.request_timeout_s,
            )
            for model in settings.openai_models
        )

    if settings.sarvam_api_key:
        engines.append(
            SarvamTranscriber(
                api_key=settings.sarvam_api_key,
                model=settings.sarvam_model,
                language=settings.sarvam_language,
                timeout_s=settings.request_timeout_s,
            )
        )

    if settings.elevenlabs_api_key:
        engines.append(
            ElevenLabsTranscriber(
                api_key=settings.elevenlabs_api_key,
                model=settings.elevenlabs_model,
                timeout_s=settings.request_timeout_s,
            )
        )

    return engines


def build_primary_transcriber(settings: SttSettings) -> Transcriber:
    """The single engine the production pipeline uses (Phase 3 onward).

    `settings.engine` (from `STT_ENGINE`) is matched as a prefix against each
    configured engine's `.name` — e.g. "sarvam" matches "sarvam/saarika:v2.5"
    without pinning the model version in two places. Left unset, this falls
    back to the first configured engine (the pre-Phase-0 behaviour), so a
    checkout with no STT_ENGINE still runs on local Whisper alone.
    """
    engines = build_transcribers(settings)
    if not engines:
        raise RuntimeError(
            "No STT engine configured. Set an API key in .env, "
            "or leave RUN_LOCAL_WHISPER=1."
        )

    if settings.engine:
        match = next((e for e in engines if e.name.startswith(settings.engine)), None)
        if match is None:
            raise RuntimeError(
                f"STT_ENGINE={settings.engine!r} but no configured engine matches "
                f"— configured: {[e.name for e in engines]}. Check its API key is set."
            )
        return match

    return engines[0]
