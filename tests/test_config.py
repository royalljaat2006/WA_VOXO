import os
from pathlib import Path

import pytest

from whatsapp_voice.config import Settings, SttSettings, WhatsAppSettings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip any real credentials from the environment so tests are hermetic."""
    for key in list(os.environ):
        if key.startswith(("WHATSAPP_", "WHISPER_", "SARVAM_", "OPENAI_",
                           "ELEVENLABS_", "RUN_LOCAL_", "STT_", "DATA_DIR")):
            monkeypatch.delenv(key, raising=False)


class TestSttSettings:
    def test_defaults(self):
        s = SttSettings.from_env()
        assert s.run_local_whisper is True
        assert s.whisper_model == "large-v3"
        assert s.whisper_language is None  # blank env → auto-detect

    def test_flag_parsing(self, monkeypatch):
        monkeypatch.setenv("RUN_LOCAL_WHISPER", "false")
        assert SttSettings.from_env().run_local_whisper is False

    def test_csv_parsing_trims_and_drops_empties(self, monkeypatch):
        monkeypatch.setenv("OPENAI_MODELS", " whisper-1 , , gpt-4o-transcribe ")
        assert SttSettings.from_env().openai_models == ("whisper-1", "gpt-4o-transcribe")


class TestWhatsAppSettings:
    def test_unconfigured_by_default(self):
        assert WhatsAppSettings.from_env().is_configured is False

    def test_configured_needs_token_and_phone_id(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
        assert WhatsAppSettings.from_env().is_configured is False  # phone id still missing
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        assert WhatsAppSettings.from_env().is_configured is True

    def test_api_version_default(self):
        assert WhatsAppSettings.from_env().api_version == "v21.0"

    def test_blank_token_is_none_not_empty_string(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "   ")
        assert WhatsAppSettings.from_env().access_token is None


class TestSettingsLoad:
    def test_real_env_overrides_are_read(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("WHISPER_MODEL", "tiny")
        # point at a non-existent env file so only real env vars apply
        settings = Settings.load(env_file=tmp_path / "nope.env")
        assert settings.stt.whisper_model == "tiny"
        assert settings.whatsapp.is_configured is False
