"""Shared contact cards — parsing Meta's contact-card payload.

The payment tools no longer take a payee from a contact card at all (payees
are spoken UPI IDs now — see `payment_tools.py`), so what is left to defend
here is just that Meta's nested, half-optional contact shape parses without
raising on any of it, and that `ContactBook` itself behaves correctly in
isolation. `ContactBook` is still populated by `PaymentHandler` when a card
arrives, but nothing in the payment-tool layer reads it any more.
"""

import pytest

from whatsapp_voice.agent.payments import ContactBook
from whatsapp_voice.whatsapp.payloads import ContactMessage, UnsupportedMessage, parse_webhook

PAYER = "919876543210"


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
