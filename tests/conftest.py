"""Shared fixtures. Test doubles themselves live in `fakes.py`."""

from pathlib import Path

import pytest

from .fakes import make_opus_bytes


@pytest.fixture
def opus_bytes(tmp_path: Path) -> bytes:
    return make_opus_bytes(tmp_path)
