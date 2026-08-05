import subprocess
from pathlib import Path

import pytest

from whatsapp_voice import audio


@pytest.fixture
def opus_file(tmp_path: Path) -> Path:
    """A real half-second Opus file, standing in for a WhatsApp voice note."""
    path = tmp_path / "note.opus"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.5", "-c:a", "libopus", str(path)],
        check=True,
    )
    return path


class TestDiscover:
    def test_finds_supported_files_sorted(self, tmp_path: Path):
        for name in ("b.opus", "a.m4a", "notes.txt"):
            (tmp_path / name).touch()
        assert [p.name for p in audio.discover(tmp_path)] == ["a.m4a", "b.opus"]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path: Path):
        assert audio.discover(tmp_path / "nope") == []


class TestToWav:
    def test_converts_to_16k_mono(self, opus_file: Path, tmp_path: Path):
        wav = audio.to_wav(opus_file, tmp_path / "cache")
        assert wav.exists() and wav.suffix == ".wav"

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True, check=True,
        )
        assert probe.stdout.strip() == "16000,1"

    def test_reuses_cached_conversion(self, opus_file: Path, tmp_path: Path):
        cache = tmp_path / "cache"
        first = audio.to_wav(opus_file, cache)
        stamp = first.stat().st_mtime_ns
        assert audio.to_wav(opus_file, cache).stat().st_mtime_ns == stamp

    def test_reconverts_when_source_is_newer(self, opus_file: Path, tmp_path: Path):
        cache = tmp_path / "cache"
        audio.to_wav(opus_file, cache)
        opus_file.touch()  # simulate replacing the recording
        # Cache is now older than source, so a fresh conversion must happen.
        assert audio.to_wav(opus_file, cache).stat().st_mtime >= opus_file.stat().st_mtime

    def test_corrupt_input_raises_audio_error(self, tmp_path: Path):
        broken = tmp_path / "broken.opus"
        broken.write_bytes(b"this is not audio")
        with pytest.raises(audio.AudioError, match="ffmpeg failed"):
            audio.to_wav(broken, tmp_path / "cache")


class TestDurationSeconds:
    def test_reads_duration(self, opus_file: Path, tmp_path: Path):
        wav = audio.to_wav(opus_file, tmp_path / "cache")
        assert audio.duration_seconds(wav) == pytest.approx(0.5, abs=0.1)
