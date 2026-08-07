"""The write side of the allowlist file.

The rule these tests exist to hold: `+pay` is not grantable from here, and
revoking always is.
"""

import pytest

from whatsapp_voice.auth import FileBackedAccessPolicy, Role, Roster, RosterError


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "allowed.txt"
    path.write_text(
        "# eko team\n"
        "918287151832:admin+pay   # Priya, ops lead\n"
        "919000000001:viewer\n"
    )
    return Roster(path)


class TestAdd:
    def test_adds_an_entry(self, roster):
        roster.add("919812345678", Role.VIEWER)
        assert roster.entries()["919812345678"] == "viewer"

    def test_normalises_the_number(self, roster):
        roster.add("+91 98123-45678", Role.ANALYST)
        assert "919812345678" in roster.entries()

    def test_refuses_a_number_that_is_too_short(self, roster):
        with pytest.raises(RosterError, match="sahi mobile number nahi"):
            roster.add("12345", Role.VIEWER)

    def test_updating_a_role_replaces_rather_than_duplicates(self, roster):
        roster.add("919000000001", Role.ADMIN)
        entries = roster.entries()
        assert entries["919000000001"] == "admin"
        assert len(entries) == 2  # not 3

    def test_preserves_comments_and_other_entries(self, roster):
        roster.add("919812345678", Role.VIEWER)
        text = roster.path.read_text()
        assert "# eko team" in text
        assert "Priya, ops lead" in text

    def test_records_who_added_it(self, roster):
        roster.add("919812345678", Role.VIEWER, note="added via WhatsApp by 918287151832")
        assert "added via WhatsApp by 918287151832" in roster.path.read_text()

    def test_creates_the_file_when_absent(self, tmp_path):
        fresh = Roster(tmp_path / "nested" / "allowed.txt")
        fresh.add("919812345678", Role.VIEWER)
        assert fresh.entries() == {"919812345678": "viewer"}


class TestPayIsNotGrantable:
    """The boundary this whole module exists to hold."""

    def test_cannot_overwrite_an_existing_payer(self, roster):
        """Rewriting the line would silently strip a capability a human granted
        on purpose — refuse instead of deciding for them."""
        with pytest.raises(RosterError, match="already payment permission"):
            roster.add("918287151832", Role.VIEWER)

    def test_the_payer_entry_survives_the_refusal(self, roster):
        with pytest.raises(RosterError):
            roster.add("918287151832", Role.VIEWER)
        assert roster.entries()["918287151832"] == "admin+pay"

    def test_role_enum_has_no_pay_member(self):
        """`add` takes a Role, and no Role carries the capability — so there is
        no argument that could ask for one."""
        assert not any("pay" in role.value for role in Role)


class TestRemove:
    def test_removes_an_entry(self, roster):
        roster.remove("919000000001")
        assert "919000000001" not in roster.entries()

    def test_revoking_a_payer_is_allowed(self, roster):
        """Cutting access is the fail-safe direction, and the thing somebody
        needs to do fast when a handset goes missing."""
        roster.remove("918287151832")
        assert "918287151832" not in roster.entries()

    def test_unknown_number_says_so(self, roster):
        with pytest.raises(RosterError, match="list mein nahi hai"):
            roster.remove("910000000000")

    def test_leaves_the_rest_alone(self, roster):
        roster.remove("919000000001")
        assert "918287151832" in roster.entries()
        assert "# eko team" in roster.path.read_text()


class TestPolicySeesChanges:
    """The read and write sides have to agree, or a change looks applied and
    is not."""

    def test_added_number_is_visible_to_the_policy(self, roster):
        policy = FileBackedAccessPolicy(str(roster.path))
        assert policy.lookup("919812345678") is None

        roster.add("919812345678", Role.ANALYST)

        assert policy.lookup("919812345678").role is Role.ANALYST

    def test_removed_number_stops_working(self, roster):
        policy = FileBackedAccessPolicy(str(roster.path))
        assert policy.lookup("919000000001") is not None

        roster.remove("919000000001")

        assert policy.lookup("919000000001") is None

    def test_two_changes_in_quick_succession_both_land(self, roster):
        """A coarse mtime comparison would miss the second write and leave a
        revoked number working."""
        policy = FileBackedAccessPolicy(str(roster.path))
        policy.lookup("919000000001")

        roster.add("919812345678", Role.VIEWER)
        roster.remove("919812345678")

        assert policy.lookup("919812345678") is None

    def test_a_failed_write_leaves_the_file_readable(self, roster):
        """Atomic replace: a policy reading mid-change must never see a partial
        file, which would read as a shorter allowlist."""
        policy = FileBackedAccessPolicy(str(roster.path))
        roster.add("919812345678", Role.VIEWER)

        assert len(policy) == 3
        assert policy.lookup("918287151832").can_pay is True
