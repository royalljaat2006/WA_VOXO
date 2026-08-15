"""Build the set of engines that current configuration can actually run.

Engines whose credentials are absent are skipped rather than erroring, so the
project works with just the local model on day one and gains engines as keys
are added.
"""

from __future__ import annotations

from ..config import SttSettings
from .base import Transcriber
from .elevenlabs import ElevenLabsTranscriber
from .fallback import FallbackTranscriber
from .google_stt import GoogleSpeechTranscriber
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

    if settings.google_credentials_path:
        engines.append(
            GoogleSpeechTranscriber(
                credentials_path=settings.google_credentials_path,
                model=settings.google_model,
                language=settings.google_language,
                alternative_languages=settings.google_alternative_languages,
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

    `settings.fallback_engine` (from `STT_ENGINE_FALLBACK`), if set, is matched
    the same way and wrapped around the primary via `FallbackTranscriber` — the
    fallback only runs when the primary raises.
    """
    engines = build_transcribers(settings)
    if not engines:
        raise RuntimeError(
            "No STT engine configured. Set an API key in .env, "
            "or leave RUN_LOCAL_WHISPER=1."
        )

    primary = _match(engines, settings.engine, "STT_ENGINE") if settings.engine else engines[0]

    if settings.fallback_engine:
        fallback = _match(engines, settings.fallback_engine, "STT_ENGINE_FALLBACK")
        if fallback is primary:
            raise RuntimeError(
                f"STT_ENGINE_FALLBACK={settings.fallback_engine!r} resolves to the same "
                f"engine as STT_ENGINE ({primary.name!r}) — pick a different one."
            )
        return FallbackTranscriber(primary, fallback)

    return primary


def _match(engines: list[Transcriber], name: str, label: str) -> Transcriber:
    match = next((e for e in engines if e.name.startswith(name)), None)
    if match is None:
        raise RuntimeError(
            f"{label}={name!r} but no configured engine matches "
            f"— configured: {[e.name for e in engines]}. Check its API key is set."
        )
    return match
