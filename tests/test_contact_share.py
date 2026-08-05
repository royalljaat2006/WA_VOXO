"""Shared contact cards — the one way a payee's number avoids speech recognition.

Two things are being defended here. That Meta's nested, half-optional contact
shape is parsed without raising on any of it. And that a number which arrived
this way is *marked* as such, because that mark is what removes the digit
read-back — get it wrong and either the safety step disappears for a dictated
number, or it nags for a verified one.
"""

import pytest

from whatsapp_voice.agent.payment_tools import (
    PrepareTransferTool,
    ResolvePayeeTool,
    payment_tools,
)
from whatsapp_voice.agent.payments import (
    ContactBook,
    PendingIntents,
    StubPaymentGateway,
    current_payer,
)
from whatsapp_voice.config import PaymentSettings
from whatsapp_voice.whatsapp.payloads import ContactMessage, UnsupportedMessage, parse_webhook

PAYER = "919876543210"
LIMITS = PaymentSettings(enabled=True)


def webhook(message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def contact_payload(**overrides) -> dict:
    base = {
        "id": "wamid.C1",
        "from": PAYER,
        "timestamp": "1",
        "type": "contacts",
        "contacts": [
            {
                "name": {"formatted_name": "Ramesh Kumar", "first_name": "Ramesh"},
                "phones": [
                    {"phone": "+91 98765 43211", "wa_id": "919876543211", "type": "CELL"}
                ],
            }
        ],
    }
    base.update(overrides)
    return base


class TestParsing:
    def test_contact_card_becomes_a_ContactMessage(self):
        [parsed] = parse_webhook(webhook(contact_payload()))
        assert isinstance(parsed, ContactMessage)
        assert parsed.only.phone == "919876543211"
        assert parsed.only.name == "Ramesh Kumar"
        assert parsed.only.is_wa_verified is True

    def test_wa_id_is_preferred_over_the_typed_phone(self):
        """`wa_id` is WhatsApp's own normalised number. The `phone` field is
        whatever the sender typed into their address book."""
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[{
            "name": {"formatted_name": "Ramesh"},
            "phones": [{"phone": "0 98765-43211", "wa_id": "919876543211"}],
        }])))
        assert parsed.only.phone == "919876543211"

    def test_falls_back_to_the_typed_phone_when_not_on_whatsapp(self):
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[{
            "name": {"formatted_name": "Ramesh"},
            "phones": [{"phone": "+91 98765 43211"}],
        }])))
        assert parsed.only.phone == "919876543211"
        assert parsed.only.is_wa_verified is False

    def test_first_name_is_used_when_there_is_no_formatted_name(self):
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[{
            "name": {"first_name": "Ramesh"},
            "phones": [{"wa_id": "919876543211"}],
        }])))
        assert parsed.only.name == "Ramesh"

    def test_contact_with_no_name_still_parses(self):
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[{
            "phones": [{"wa_id": "919876543211"}],
        }])))
        assert parsed.only.name is None
        assert parsed.only.phone == "919876543211"

    def test_several_contacts_are_all_kept(self):
        """Picking one would be the confident guess this whole flow avoids."""
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[
            {"name": {"formatted_name": "Ramesh"}, "phones": [{"wa_id": "919876543211"}]},
            {"name": {"formatted_name": "Suresh"}, "phones": [{"wa_id": "919876543212"}]},
        ])))
        assert len(parsed.contacts) == 2
        assert parsed.only is None


class TestMalformedCardsNeverRaise:
    """A webhook handler that 500s makes Meta retry the same bad payload forever."""

    @pytest.mark.parametrize("contacts", [
        [],
        [{}],
        [{"name": {"formatted_name": "Ramesh"}}],       # no phones at all
        [{"phones": []}],
        [{"phones": [{}]}],                              # phone entry with nothing in it
        [{"phones": [{"phone": ""}]}],
        [{"phones": [{"phone": "no digits here"}]}],
        [{"phones": "not-a-list"}],
        ["not-a-dict"],
        "not-a-list",
    ])
    def test_unusable_card_becomes_unsupported_not_an_exception(self, contacts):
        [parsed] = parse_webhook(webhook(contact_payload(contacts=contacts)))
        assert isinstance(parsed, UnsupportedMessage)
        assert parsed.kind == "contacts"

    def test_one_good_contact_among_broken_ones_survives(self):
        [parsed] = parse_webhook(webhook(contact_payload(contacts=[
            {"phones": [{}]},
            {"name": {"formatted_name": "Ramesh"}, "phones": [{"wa_id": "919876543211"}]},
        ])))
        assert isinstance(parsed, ContactMessage)
        assert parsed.only.phone == "919876543211"


class TestContactBook:
    def test_remembers_and_finds(self):
        book = ContactBook()
        book.remember(PAYER, "919876543211", "Ramesh Kumar")
        assert book.lookup(PAYER, "919876543211") == (True, "Ramesh Kumar")

    def test_is_scoped_to_one_payer(self):
        """One person's address book must not vouch for another's digits."""
        book = ContactBook()
        book.remember(PAYER, "919876543211", "Ramesh")
        assert book.lookup("919000000001", "919876543211") == (False, None)

    def test_unknown_number_is_not_found(self):
        assert ContactBook().lookup(PAYER, "919876543211") == (False, None)

    def test_entries_expire(self):
        """A card shared an hour ago is not evidence about the number being
        spoken now."""
        book = ContactBook(ttl_s=-1)
        book.remember(PAYER, "919876543211", "Ramesh")
        assert book.lookup(PAYER, "919876543211") == (False, None)

    def test_formatting_is_ignored_on_both_sides(self):
        book = ContactBook()
        book.remember(PAYER, "+91 98765 43211", "Ramesh")
        assert book.lookup(PAYER, "919876543211")[0] is True

    def test_a_number_with_no_digits_is_not_stored(self):
        book = ContactBook()
        book.remember(PAYER, "not a number")
        assert book.lookup(PAYER, "") == (False, None)


class TestSharedNumberSkipsTheReadBack:
    @pytest.fixture
    def gateway(self):
        return StubPaymentGateway(pin="1234")

    @pytest.fixture
    def book(self):
        current_payer.set(PAYER)
        return ContactBook()

    def test_dictated_unknown_number_asks_for_a_read_back(self, gateway, book):
        result = ResolvePayeeTool(gateway, book).run(payee="919111222333")
        assert "read_back_digits" in result
        assert "source=dictated" in result

    def test_shared_number_does_not(self, gateway, book):
        """The whole point: WhatsApp verified these digits, so there is nothing
        for a human to catch by hearing them again."""
        book.remember(PAYER, "919111222333", "Naya Aadmi")
        result = ResolvePayeeTool(gateway, book).run(payee="919111222333")

        assert "read_back_digits" not in result
        assert "source=contact_card" in result
        assert "name=Naya Aadmi" in result

    def test_prepare_marks_the_source(self, gateway, book):
        book.remember(PAYER, "919111222333", "Naya Aadmi")
        tool = PrepareTransferTool(gateway, PendingIntents(), LIMITS, book)
        assert "payee_source=contact_card" in tool.run(payee="919111222333", amount="400")

    def test_directory_name_beats_the_senders_address_book(self, gateway, book):
        """eko's own record is authoritative; the sender may have saved the
        contact under a nickname."""
        book.remember(PAYER, "919876543210", "Ramesh bhai")
        result = ResolvePayeeTool(gateway, book).run(payee="919876543210")
        assert "name=Ramesh Kumar" in result

    def test_first_time_cap_still_applies_to_a_shared_contact(self, gateway, book):
        """Sharing a card proves the digits, not that the person is known. A new
        payee is still a new payee."""
        from whatsapp_voice.agent.tools import ToolError

        book.remember(PAYER, "919111222333", "Naya Aadmi")
        tool = PrepareTransferTool(gateway, PendingIntents(), LIMITS, book)
        with pytest.raises(ToolError, match="first transfer"):
            tool.run(payee="919111222333", amount="2000")

    def test_tools_built_together_share_one_book(self, gateway):
        """Separate instances would silently lose the one fact that makes a
        shared contact safer than a dictated number."""
        book = ContactBook()
        resolve, prepare = payment_tools(gateway, PendingIntents(), LIMITS, book)
        book.remember(PAYER, "919111222333", "Naya Aadmi")
        current_payer.set(PAYER)

        assert "source=contact_card" in resolve.run(payee="919111222333")
        assert "payee_source=contact_card" in prepare.run(payee="919111222333", amount="400")


class TestContactOnlyMode:
    def test_dictated_number_is_refused(self):
        from whatsapp_voice.agent.tools import ToolError

        current_payer.set(PAYER)
        tool = PrepareTransferTool(
            StubPaymentGateway(), PendingIntents(),
            PaymentSettings(enabled=True, payee_input_mode="contact_only"),
            ContactBook(),
        )
        with pytest.raises(ToolError, match="shared contacts"):
            tool.run(payee="919111222333", amount="400")

    def test_shared_number_is_allowed(self):
        current_payer.set(PAYER)
        book = ContactBook()
        book.remember(PAYER, "919111222333", "Naya Aadmi")
        pending = PendingIntents()
        tool = PrepareTransferTool(
            StubPaymentGateway(), pending,
            PaymentSettings(enabled=True, payee_input_mode="contact_only"),
            book,
        )
        tool.run(payee="919111222333", amount="400")
        assert pending.get(PAYER) is not None

    def test_known_directory_payee_is_allowed(self):
        """A payee eko already knows was never dictated in the risky sense —
        the name resolved to a record, not to loose digits."""
        current_payer.set(PAYER)
        pending = PendingIntents()
        tool = PrepareTransferTool(
            StubPaymentGateway(), pending,
            PaymentSettings(enabled=True, payee_input_mode="contact_only"),
            ContactBook(),
        )
        tool.run(payee="Ramesh", amount="2000")
        assert pending.get(PAYER) is not None
