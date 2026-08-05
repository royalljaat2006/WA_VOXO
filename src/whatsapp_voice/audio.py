"""Audio normalisation.

WhatsApp voice notes arrive as OGG/Opus. Engines disagree on what they accept,
so everything is converted to 16kHz mono WAV first — that also guarantees each
engine in a benchmark sees byte-identical input.

Phase 3 reuses this unchanged for audio downloaded from the Cloud API.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SUPPORTED_EXTENSIONS = frozenset(
    # .webm is what a browser's MediaRecorder produces — needed by the demo UI,
    # never by WhatsApp.
    {".opus", ".ogg", ".oga", ".m4a", ".mp3", ".wav", ".aac", ".flac", ".amr", ".webm"}
)

TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1


class AudioError(RuntimeError):
    """Raised when audio cannot be read or converted."""


def require_ffmpeg() -> None:
    """Fail early with an actionable message rather than deep inside a run."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise AudioError(
            f"{', '.join(missing)} not found on PATH. Install with: brew install ffmpeg"
        )


def to_wav(source: Path, cache_dir: Path) -> Path:
    """Convert to 16kHz mono WAV, caching the result next to previous runs.

    The cached file is reused only if it is newer than the source, so replacing
    an audio file re-converts it.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{source.stem}.wav"

    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(source),
            "-ar", str(TARGET_SAMPLE_RATE),
            "-ac", str(TARGET_CHANNELS),
            str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffmpeg failed on {source.name}: {result.stderr.strip()}")
    return target


def duration_seconds(audio: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe failed on {audio.name}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def discover(directory: Path) -> list[Path]:
    """Return supported audio files in a directory, sorted by name."""
    if not directory.exists():
        return []
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
