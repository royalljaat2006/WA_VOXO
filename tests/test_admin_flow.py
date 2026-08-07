"""Super-user allowlist commands.

The two properties worth protecting here: `+pay` cannot be granted by a message,
and the commands are unreachable by the model.
"""

import json

import pytest

from whatsapp_voice.audit import AuditTrail
from whatsapp_voice.auth import AdminFlow, Role, Roster, looks_like_command

SUPER = "918287151832"


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "allowed.txt"
    path.write_text(
        "# eko team\n"
        "918287151832:admin+pay   # Priya, ops lead\n"
        "919000000001:viewer\n"
    )
    return Roster(path)


@pytest.fixture
def audit(tmp_path):
    return AuditTrail(tmp_path / "audit.jsonl")


@pytest.fixture
def flow(roster, audit):
    return AdminFlow(roster, audit)


def records(audit):
    return [json.loads(line) for line in audit._path.read_text().splitlines()]


class TestLooksLikeCommand:
    def test_slash_prefixed_text(self):
        assert looks_like_command("/adduser 919812345678") is True

    @pytest.mark.parametrize("text", [
        "aaj kitne campaign gaye", "", "adduser 919812345678", "kya haal hai /adduser",
    ])
    def test_ordinary_text_is_not_a_command(self, text):
        assert looks_like_command(text) is False


class TestAddUser:
    def test_adds_with_an_explicit_role(self, flow, roster):
        outcome = flow.handle(SUPER, "/adduser 919812345678 analyst")
        assert "919812345678" in outcome.reply
        assert roster.entries()["919812345678"] == "analyst"

    def test_defaults_to_viewer(self, flow, roster):
        """Least privilege: a forgotten role must not hand out more than meant."""
        flow.handle(SUPER, "/adduser 919812345678")
        assert roster.entries()["919812345678"] == "viewer"

    def test_reply_echoes_the_number_so_a_typo_is_visible(self, flow):
        outcome = flow.handle(SUPER, "/adduser 919812345678")
        assert "919812345678" in outcome.reply
        assert "/removeuser 919812345678" in outcome.reply

    def test_reply_states_they_cannot_pay(self, flow):
        outcome = flow.handle(SUPER, "/adduser 919812345678 admin")
        assert "nahi bhej sakta" in outcome.reply

    def test_bad_role_is_refused_and_lists_the_valid_ones(self, flow, roster):
        outcome = flow.handle(SUPER, "/adduser 919812345678 superadmin")
        assert "viewer" in outcome.reply and "analyst" in outcome.reply
        assert "919812345678" not in roster.entries()

    def test_missing_number_asks_for_one(self, flow):
        assert "Number batao" in flow.handle(SUPER, "/adduser").reply

    def test_records_the_change(self, flow, audit):
        flow.handle(SUPER, "/adduser 919812345678 viewer")
        entry = records(audit)[-1]
        assert entry["event"] == "user_added"
        assert entry["actor"] == SUPER
        assert entry["subject"] == "919812345678"
        assert entry["role"] == "viewer"


class TestPayCannotBeGranted:
    """A borrowed handset must not be able to mint a payer."""

    @pytest.mark.parametrize("command", [
        "/adduser 919812345678 admin+pay",
        "/adduser 919812345678 viewer+pay",
        "/adduser 919812345678 +pay",
    ])
    def test_refused(self, flow, roster, command):
        outcome = flow.handle(SUPER, command)
        assert "nahi de sakta" in outcome.reply
        assert "919812345678" not in roster.entries()

    def test_refusal_explains_why_and_offers_the_alternative(self, flow):
        outcome = flow.handle(SUPER, "/adduser 919812345678 admin+pay")
        assert "phone kiske haath mein hai" in outcome.reply
        assert "/adduser 919812345678 admin" in outcome.reply

    def test_nothing_is_recorded_for_a_refusal(self, flow, audit):
        flow.handle(SUPER, "/adduser 919812345678 admin+pay")
        assert not audit._path.exists() or records(audit) == []


class TestRemoveUser:
    def test_removes(self, flow, roster):
        outcome = flow.handle(SUPER, "/removeuser 919000000001")
        assert "hata diya" in outcome.reply
        assert "919000000001" not in roster.entries()

    def test_revoking_a_payer_works_and_says_so(self, flow, roster):
        """The emergency capability: cut a payer's access from a phone."""
        outcome = flow.handle(SUPER, "/removeuser 918287151832")
        assert "918287151832" not in roster.entries()
        assert "payment permission thi" in outcome.reply

    def test_unknown_number(self, flow):
        assert "nahi hai" in flow.handle(SUPER, "/removeuser 910000000000").reply

    def test_records_the_change_including_whether_they_could_pay(self, flow, audit):
        flow.handle(SUPER, "/removeuser 918287151832")
        entry = records(audit)[-1]
        assert entry["event"] == "user_removed"
        assert entry["was_payer"] is True


class TestListUsers:
    def test_lists_everyone_with_their_role(self, flow):
        reply = flow.handle(SUPER, "/listusers").reply
        assert "918287151832" in reply
        assert "919000000001" in reply
        assert "2 number(s)" in reply

    def test_marks_who_can_pay(self, flow):
        assert "💰" in flow.handle(SUPER, "/listusers").reply

    def test_empty_list_is_stated_plainly(self, tmp_path):
        empty = AdminFlow(Roster(tmp_path / "none.txt"))
        assert "khali" in empty.handle(SUPER, "/listusers").reply


class TestUnknownCommands:
    def test_typo_gets_help_rather_than_reaching_the_model(self, flow):
        """Forwarding '/addusr 91…' to a model invites it to improvise a
        confirmation for something that never happened."""
        outcome = flow.handle(SUPER, "/addusr 919812345678")
        assert outcome.forward is False
        assert "/adduser" in outcome.reply

    def test_help(self, flow):
        assert "/adduser" in flow.handle(SUPER, "/help").reply

    def test_help_mentions_that_pay_is_server_only(self, flow):
        assert "server" in flow.handle(SUPER, "/help").reply

    def test_non_command_text_is_passed_through(self, flow):
        outcome = flow.handle(SUPER, "aaj kitne campaign gaye")
        assert outcome.forward is True
        assert outcome.reply is None


class TestWriteFailures:
    def test_a_write_error_is_reported_not_swallowed(self, flow, monkeypatch, roster):
        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Roster, "add", explode)
        outcome = flow.handle(SUPER, "/adduser 919812345678 viewer")
        assert "nahi ho payi" in outcome.reply

    def test_a_failed_add_is_not_recorded_as_done(self, flow, monkeypatch, audit):
        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Roster, "add", explode)
        flow.handle(SUPER, "/adduser 919812345678 viewer")
        assert not audit._path.exists() or records(audit) == []


class TestNoModelPath:
    """The rule that keeps a prompt injected into a transcript harmless."""

    def test_admin_flow_is_not_a_tool(self):
        from whatsapp_voice.agent import campaign_tools, payment_tools

        names = [
            getattr(obj, "name", "")
            for module in (campaign_tools, payment_tools)
            for obj in vars(module).values()
            if isinstance(obj, type)
        ]
        for forbidden in ("add_user", "adduser", "remove_user", "grant"):
            assert forbidden not in names

    def test_role_cannot_express_the_pay_capability(self):
        """`AdminFlow._add` builds a Role, so there is no path from any command
        string to a granted capability."""
        with pytest.raises(ValueError):
            Role("admin+pay")
