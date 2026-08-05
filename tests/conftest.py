"""Shared fixtures. Test doubles themselves live in `fakes.py`."""

from pathlib import Path

import pytest

from whatsapp_voice.auth.policy import Caller, Role, current_caller

from .fakes import make_opus_bytes


@pytest.fixture
def opus_bytes(tmp_path: Path) -> bytes:
    return make_opus_bytes(tmp_path)


@pytest.fixture(autouse=True)
def authorised_caller():
    """Every test starts as a caller who may pay, and ends with that reset.

    Without this, every payment test would have to restate the authorisation
    setup that is not what it is testing. The refusal paths are covered
    deliberately in `test_payer_role.py`, which overrides this — so the check
    itself cannot rot unnoticed.

    The reset matters more than the setup: ContextVars leak between tests in the
    same thread, and a caller left set by one test silently authorising the next
    is exactly the kind of bug this whole area exists to prevent.
    """
    token = current_caller.set(
        Caller(number="919876543210", role=Role.ADMIN, can_pay=True)
    )
    yield
    current_caller.reset(token)
