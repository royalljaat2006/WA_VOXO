import pytest

from whatsapp_voice.auth import (
    AccessGuard,
    AccessPolicy,
    Decision,
    FileBackedAccessPolicy,
    Role,
    SlidingWindowRateLimiter,
    normalise_number,
)
from whatsapp_voice.config import AuthSettings


class TestNormaliseNumber:
    @pytest.mark.parametrize("raw", [
        "919876543210", "+919876543210", "+91 98765 43210",
        "+91-98765-43210", " 91 (98765) 43210 ",
    ])
    def test_all_formats_collapse_to_digits(self, raw):
        assert normalise_number(raw) == "919876543210"

    def test_empty_input_is_safe(self):
        assert normalise_number("") == ""
        assert normalise_number(None) == ""


class TestAccessPolicyParsing:
    def test_number_with_role(self):
        policy = AccessPolicy.parse("919876543210:admin")
        assert policy.lookup("919876543210").role is Role.ADMIN

    def test_bare_number_gets_least_privilege(self):
        """A config typo must not silently grant more than intended."""
        assert AccessPolicy.parse("919876543210").lookup("919876543210").role is Role.VIEWER

    def test_multiple_entries(self):
        policy = AccessPolicy.parse(
            "919876543210:admin, 919000000001:analyst ,919000000002"
        )
        assert len(policy) == 3
        assert policy.lookup("919000000001").role is Role.ANALYST
        assert policy.lookup("919000000002").role is Role.VIEWER

    def test_formatting_in_config_is_forgiven(self):
        policy = AccessPolicy.parse("+91 98765-43210:admin")
        assert policy.lookup("919876543210") is not None

    def test_unknown_number_is_none(self):
        assert AccessPolicy.parse("919876543210:admin").lookup("910000000000") is None

    def test_empty_spec_is_empty_policy(self):
        assert AccessPolicy.parse("").is_empty is True

    def test_bad_role_fails_loudly(self):
        """A mistyped role must not quietly downgrade access."""
        with pytest.raises(ValueError, match="Unknown role"):
            AccessPolicy.parse("919876543210:analsyt")

    def test_entry_without_a_number_is_rejected(self):
        with pytest.raises(ValueError, match="no phone number"):
            AccessPolicy.parse(":admin")

    def test_custom_default_role(self):
        policy = AccessPolicy.parse("919876543210", default_role=Role.ANALYST)
        assert policy.lookup("919876543210").role is Role.ANALYST


class TestFileBackedAccessPolicy:
    """Lets a team that turns over often add/remove a number without a
    redeploy — the file is re-read only when it actually changes."""

    def test_reads_the_file(self, tmp_path):
        path = tmp_path / "allowed.txt"
        path.write_text("919876543210:admin+pay\n")

        policy = FileBackedAccessPolicy(str(path))
        caller = policy.lookup("919876543210")

        assert caller.role is Role.ADMIN
        assert caller.can_pay is True

    def test_blank_lines_and_comments_are_ignored(self, tmp_path):
        path = tmp_path / "allowed.txt"
        path.write_text(
            "# eko team\n"
            "\n"
            "919876543210:admin+pay   # Priya, ops lead\n"
            "919000000001\n"
        )

        policy = FileBackedAccessPolicy(str(path))

        assert len(policy) == 2
        assert policy.lookup("919000000001").role is Role.VIEWER  # bare -> default

    def test_missing_file_denies_everyone_rather_than_crashing(self, tmp_path):
        policy = FileBackedAccessPolicy(str(tmp_path / "does-not-exist.txt"))
        assert policy.is_empty is True
        assert policy.lookup("919876543210") is None

    def test_picks_up_a_later_edit(self, tmp_path):
        path = tmp_path / "allowed.txt"
        path.write_text("919876543210:admin\n")
        policy = FileBackedAccessPolicy(str(path))
        assert policy.lookup("919000000001") is None

        path.write_text("919876543210:admin\n919000000001:viewer\n")
        assert policy.lookup("919000000001").role is Role.VIEWER

    def test_does_not_reread_when_unchanged(self, tmp_path, monkeypatch):
        path = tmp_path / "allowed.txt"
        path.write_text("919876543210:admin\n")
        policy = FileBackedAccessPolicy(str(path))
        policy.lookup("919876543210")  # first read

        calls = []
        monkeypatch.setattr(path.__class__, "read_text",
                             lambda self: calls.append(1) or "919876543210:admin\n")
        policy.lookup("919876543210")

        assert calls == []  # mtime unchanged -> read_text must not run again

    def test_keeps_last_known_list_if_file_becomes_unreadable(self, tmp_path):
        path = tmp_path / "allowed.txt"
        path.write_text("919876543210:admin\n")
        policy = FileBackedAccessPolicy(str(path))
        policy.lookup("919876543210")

        path.unlink()

        assert policy.lookup("919876543210").role is Role.ADMIN


class TestRoles:
    def test_ordering(self):
        assert Role.ADMIN.rank > Role.ANALYST.rank > Role.VIEWER.rank

    def test_higher_role_allows_lower(self):
        assert Role.ADMIN.allows(Role.VIEWER) is True
        assert Role.ANALYST.allows(Role.ANALYST) is True

    def test_lower_role_does_not_allow_higher(self):
        assert Role.VIEWER.allows(Role.ANALYST) is False

    def test_there_is_no_write_role(self):
        """Read-only by design — number identity cannot authorise a state change."""
        assert {r.value for r in Role} == {"viewer", "analyst", "admin"}


class TestRateLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = SlidingWindowRateLimiter(limit=3, window_s=60)
        assert [limiter.check("n") for _ in range(3)] == [True, True, True]

    def test_blocks_past_the_limit(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_s=60)
        limiter.check("n")
        limiter.check("n")
        assert limiter.check("n") is False

    def test_keys_are_independent(self):
        limiter = SlidingWindowRateLimiter(limit=1, window_s=60)
        assert limiter.check("a") is True
        assert limiter.check("b") is True

    def test_window_slides(self, monkeypatch):
        import whatsapp_voice.auth.rate_limit as mod

        clock = {"t": 100.0}
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

        limiter = SlidingWindowRateLimiter(limit=2, window_s=60)
        limiter.check("n")
        limiter.check("n")
        assert limiter.check("n") is False

        clock["t"] += 61
        assert limiter.check("n") is True

    def test_rejected_attempts_do_not_extend_the_lockout(self, monkeypatch):
        """A limit, not a lockout: retrying during cooldown must not reset it."""
        import whatsapp_voice.auth.rate_limit as mod

        clock = {"t": 100.0}
        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

        limiter = SlidingWindowRateLimiter(limit=1, window_s=60)
        limiter.check("n")

        for _ in range(5):          # user keeps trying
            clock["t"] += 5
            assert limiter.check("n") is False

        clock["t"] += 40            # 65s after the ONE accepted event
        assert limiter.check("n") is True

    def test_remaining_does_not_consume(self):
        limiter = SlidingWindowRateLimiter(limit=3, window_s=60)
        assert limiter.remaining("n") == 3
        assert limiter.remaining("n") == 3
        limiter.check("n")
        assert limiter.remaining("n") == 2

    def test_retry_after_is_zero_when_not_limited(self):
        limiter = SlidingWindowRateLimiter(limit=5, window_s=60)
        limiter.check("n")
        assert limiter.retry_after_s("n") == 0.0

    def test_retry_after_is_positive_when_limited(self):
        limiter = SlidingWindowRateLimiter(limit=1, window_s=60)
        limiter.check("n")
        assert 0 < limiter.retry_after_s("n") <= 60

    def test_rejects_nonsense_limit(self):
        with pytest.raises(ValueError, match="at least 1"):
            SlidingWindowRateLimiter(limit=0)

    def test_capacity_evicts_oldest_keys(self):
        limiter = SlidingWindowRateLimiter(limit=5, capacity=2)
        for key in ("a", "b", "c"):
            limiter.check(key)
        assert limiter.remaining("a") == 5  # evicted, so back to full


def guard(spec="919876543210:admin", **overrides):
    settings = AuthSettings(allowed_numbers=spec, **overrides)
    return AccessGuard.from_settings(settings)


class TestAccessGuard:
    def test_allows_a_listed_number(self):
        result = guard().check("919876543210")
        assert result.allowed is True
        assert result.caller.role is Role.ADMIN

    def test_denies_an_unlisted_number(self):
        result = guard().check("910000000000")
        assert result.allowed is False
        assert result.decision is Decision.UNKNOWN_NUMBER

    def test_denial_message_reveals_nothing(self):
        """A prober must not learn that they found an eko internal system."""
        message = guard().check("910000000000").message.lower()
        for leak in ("eko", "campaign", "allowlist", "authoris", "authoriz", "admin"):
            assert leak not in message

    def test_formatting_differences_still_match(self):
        assert guard(spec="+91 98765-43210:admin").check("919876543210").allowed is True

    def test_empty_allowlist_denies_everyone(self):
        """The safe failure: unconfigured means closed, not open."""
        assert guard(spec="").check("919876543210").allowed is False

    def test_rate_limit_applies_to_known_numbers(self):
        g = guard(rate_limit=2)
        assert g.check("919876543210").allowed is True
        assert g.check("919876543210").allowed is True

        third = g.check("919876543210")
        assert third.allowed is False
        assert third.decision is Decision.RATE_LIMITED
        assert "try again" in third.message.lower()

    def test_rate_limited_reply_gives_a_wait_time(self):
        g = guard(rate_limit=1, rate_limit_window_s=600)
        g.check("919876543210")
        assert "minute" in g.check("919876543210").message

    def test_unknown_numbers_do_not_consume_rate_budget(self):
        """Identity is checked first, so unknown senders cannot fill the limiter."""
        g = guard(spec="919876543210:admin", rate_limit=1)
        for _ in range(50):
            g.check("910000000000")
        assert g.check("919876543210").allowed is True

    def test_disabled_auth_allows_everything(self):
        result = guard(spec="", enabled=False).check("910000000000")
        assert result.allowed is True
        assert result.caller.role is Role.ADMIN

    def test_disabled_auth_is_logged_as_a_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            guard(spec="", enabled=False)
        assert "AUTH IS DISABLED" in caplog.text

    def test_empty_allowlist_is_logged_as_a_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            guard(spec="")
        assert "Allowlist is empty" in caplog.text

    def test_logs_mask_the_number(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            guard().check("919876543210999")
        assert "919876543210999" not in caplog.text
        assert "…0999" in caplog.text

    def test_allowed_numbers_file_takes_precedence_when_set(self, tmp_path):
        path = tmp_path / "allowed.txt"
        path.write_text("919000000001:admin+pay\n")
        settings = AuthSettings(
            allowed_numbers="919876543210:admin", allowed_numbers_file=str(path)
        )

        result = AccessGuard.from_settings(settings).check("919876543210")

        assert result.decision is Decision.UNKNOWN_NUMBER  # inline spec ignored
        assert AccessGuard.from_settings(settings).check("919000000001").allowed is True


class TestAuthSettings:
    def test_defaults_are_closed(self):
        settings = AuthSettings()
        assert settings.enabled is True
        assert settings.allowed_numbers == ""
        assert settings.allowed_numbers_file == ""

    def test_reads_allowed_numbers_file_from_env(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS_FILE", "/srv/allowed.txt")
        assert AuthSettings.from_env().allowed_numbers_file == "/srv/allowed.txt"

    def test_no_superusers_by_default(self):
        assert AuthSettings().superusers == frozenset()

    def test_superusers_are_normalised(self, monkeypatch):
        """A number formatted in .env must match the bare digits a webhook sends."""
        monkeypatch.setenv("WHATSAPP_SUPERUSER_NUMBERS", "+91 82871-51832, 919000000001")
        assert AuthSettings.from_env().superusers == {"918287151832", "919000000001"}

    def test_superusers_are_env_only_never_from_the_file(self, monkeypatch):
        """If this list lived in the file the commands can edit, a super-user
        could mint more super-users from a phone."""
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS_FILE", "/srv/allowed.txt")
        monkeypatch.delenv("WHATSAPP_SUPERUSER_NUMBERS", raising=False)
        assert AuthSettings.from_env().superusers == frozenset()

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "919876543210:analyst")
        monkeypatch.setenv("AUTH_RATE_LIMIT", "5")
        settings = AuthSettings.from_env()
        assert settings.rate_limit == 5
        assert "analyst" in settings.allowed_numbers

    def test_auth_can_be_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "false")
        assert AuthSettings.from_env().enabled is False
