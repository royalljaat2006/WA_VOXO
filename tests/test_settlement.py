"""Following a transfer eko accepted but has not settled.

The state this covers is the one that actually cost money: a real transfer came
back `status: 0` / `tx_status: "2"`, was reported to the payer as done, and
reversed hours later. eko has no webhook, so the only way to learn the outcome
is to keep asking — and the only way that helps is if the payer is eventually
told.

No network and no clock: `poll_once` takes the gateway and the deadline as
arguments precisely so every branch below is reachable in a unit test.
"""

import pytest

from whatsapp_voice.agent.payments import TransferResult
from whatsapp_voice.agent.settlement import (
    PendingSettlement,
    SettlementQueue,
    poll_once,
)
from whatsapp_voice.store import MemoryStore
from whatsapp_voice.whatsapp.settlement_poller import SettlementPoller

SIX_HOURS = 21_600.0


def settlement(**overrides) -> PendingSettlement:
    base = dict(
        reference="TXN00001", payer="910000000001",
        amount_paise=1000, payee_display="90000 00000",
    )
    base.update(overrides)
    return PendingSettlement(**base)


class FakeGateway:
    """Answers inquiries from a script. `None` raises, like an unreachable eko."""

    def __init__(self, *results):
        self._results = list(results)
        self.asked: list[str] = []

    def inquire(self, reference: str, payer: str) -> TransferResult:
        self.asked.append(reference)
        result = self._results.pop(0) if self._results else TransferResult(
            ok=False, pending=True
        )
        if result is None:
            raise RuntimeError("eko unreachable")
        return result


class FakeClient:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self._fail = fail

    def send_text(self, to: str, body: str) -> str:
        if self._fail:
            raise RuntimeError("WhatsApp down")
        self.sent.append((to, body))
        return "wamid.X"


@pytest.fixture
def queue():
    return SettlementQueue(MemoryStore())


class TestQueue:
    def test_a_settlement_survives_a_round_trip(self, queue):
        queue.add(settlement())
        [stored] = queue.all()

        assert stored.reference == "TXN00001"
        assert stored.amount_paise == 1000
        assert stored.payer == "910000000001"

    def test_adding_the_same_reference_twice_does_not_duplicate_it(self, queue):
        """A retry of the same transfer must not mean two notifications."""
        queue.add(settlement())
        queue.add(settlement())
        assert len(queue.all()) == 1

    def test_remove_takes_it_out_of_the_index_too(self, queue):
        queue.add(settlement())
        queue.remove("TXN00001")

        assert queue.all() == []
        assert len(queue) == 0

    def test_touch_counts_the_attempt(self, queue):
        queue.add(settlement())
        queue.touch(queue.all()[0])

        assert queue.all()[0].attempts == 1

    def test_an_index_entry_with_no_record_is_dropped(self, queue):
        """The two can drift apart on a TTL expiry. A dangling reference must
        not be carried, and must not crash the sweep."""
        store = MemoryStore()
        q = SettlementQueue(store)
        q.add(settlement())
        store.delete("settlement:TXN00001")

        assert q.all() == []

    def test_an_unreadable_record_is_dropped_not_raised(self):
        store = MemoryStore()
        q = SettlementQueue(store)
        q.add(settlement())
        store.set("settlement:TXN00001", "not json")

        assert q.all() == []


class TestPollOnce:
    def test_settled_tells_the_payer_it_arrived(self):
        gw = FakeGateway(TransferResult(ok=True, reference="TXN00001"))
        outcome = poll_once(settlement(), gw, SIX_HOURS)

        assert outcome.resolved
        assert outcome.settled is True
        assert "10.00" in outcome.message
        assert "TXN00001" in outcome.message

    def test_failed_tells_the_payer_it_did_not(self):
        gw = FakeGateway(TransferResult(ok=False, message="Beneficiary invalid"))
        outcome = poll_once(settlement(), gw, SIX_HOURS)

        assert outcome.resolved
        assert outcome.settled is False
        assert "Beneficiary invalid" in outcome.message

    def test_still_pending_resolves_nothing_and_says_nothing(self):
        """The common case. Sending "still waiting" every minute would be worse
        than silence."""
        gw = FakeGateway(TransferResult(ok=False, pending=True))
        outcome = poll_once(settlement(), gw, SIX_HOURS)

        assert not outcome.resolved
        assert outcome.message is None

    def test_an_unreachable_gateway_is_a_retry_not_a_verdict(self):
        """eko being down says nothing about where the money is. Concluding
        'failed' here would tell the payer their transfer bounced when it may
        well have landed."""
        outcome = poll_once(settlement(), FakeGateway(None), SIX_HOURS)

        assert not outcome.resolved
        assert outcome.settled is None

    def test_giving_up_tells_the_payer_rather_than_going_quiet(self):
        old = settlement(created_at=0.0)  # epoch: far past any deadline
        gw = FakeGateway(TransferResult(ok=False, pending=True))
        outcome = poll_once(old, gw, SIX_HOURS)

        assert outcome.resolved
        assert outcome.settled is None, "unknown is not failed — eko never said failed"
        assert "check kar" in outcome.message

    def test_it_gives_up_even_when_the_gateway_is_unreachable(self):
        """Otherwise an eko outage means the payer is never told anything."""
        old = settlement(created_at=0.0)
        outcome = poll_once(old, FakeGateway(None), SIX_HOURS)

        assert outcome.resolved


class TestSweep:
    def poller(self, queue, gateway, client, **overrides):
        kwargs = dict(interval_s=60.0, give_up_after_s=SIX_HOURS)
        kwargs.update(overrides)
        return SettlementPoller(queue, gateway, client, **kwargs)

    def test_a_settled_transfer_is_notified_and_dequeued(self, queue):
        queue.add(settlement())
        client = FakeClient()
        resolved = self.poller(
            queue, FakeGateway(TransferResult(ok=True, reference="TXN00001")), client
        ).sweep()

        assert resolved == 1
        assert queue.all() == []
        [(to, body)] = client.sent
        assert to == "910000000001"
        assert "Transaction successful" in body

    def test_a_pending_transfer_stays_queued_and_is_not_notified(self, queue):
        queue.add(settlement())
        client = FakeClient()
        self.poller(queue, FakeGateway(TransferResult(ok=False, pending=True)), client).sweep()

        assert len(queue.all()) == 1
        assert client.sent == []

    def test_a_resolved_transfer_is_never_notified_twice(self, queue):
        """The failure that would flood a payer: dequeue must happen, and a
        second sweep must find nothing."""
        queue.add(settlement())
        client = FakeClient()
        gw = FakeGateway(
            TransferResult(ok=True, reference="TXN00001"),
            TransferResult(ok=True, reference="TXN00001"),
        )
        poller = self.poller(queue, gw, client)
        poller.sweep()
        poller.sweep()

        assert len(client.sent) == 1

    def test_a_failed_send_does_not_requeue_and_re_notify_forever(self, queue):
        """Dequeue happens before the send. One lost message beats an infinite
        loop of them."""
        queue.add(settlement())
        poller = self.poller(
            queue, FakeGateway(TransferResult(ok=True)), FakeClient(fail=True)
        )
        poller.sweep()

        assert queue.all() == []

    def test_an_empty_queue_asks_eko_nothing(self, queue):
        gw = FakeGateway()
        assert self.poller(queue, gw, FakeClient()).sweep() == 0
        assert gw.asked == []

    def test_several_settlements_resolve_independently(self, queue):
        queue.add(settlement(reference="A"))
        queue.add(settlement(reference="B"))
        client = FakeClient()
        gw = FakeGateway(
            TransferResult(ok=True, reference="A"),
            TransferResult(ok=False, pending=True),
        )
        resolved = self.poller(queue, gw, client).sweep()

        assert resolved == 1
        assert [s.reference for s in queue.all()] == ["B"]

    def test_one_bad_settlement_does_not_stop_the_rest(self, queue):
        """A send that raises on the first must not skip the second."""
        queue.add(settlement(reference="A"))
        queue.add(settlement(reference="B"))
        gw = FakeGateway(
            TransferResult(ok=True, reference="A"),
            TransferResult(ok=True, reference="B"),
        )
        assert self.poller(queue, gw, FakeClient(fail=True)).sweep() == 2

    def test_the_store_lease_stops_a_second_worker_double_notifying(self, queue):
        """With several workers only one may sweep — otherwise each notifies the
        same payer about the same transfer."""
        store = MemoryStore()
        q = SettlementQueue(store)
        q.add(settlement())
        client = FakeClient()

        first = self.poller(
            q, FakeGateway(TransferResult(ok=True)), client, store=store
        )
        second = self.poller(
            q, FakeGateway(TransferResult(ok=True)), client, store=store
        )
        # The lease is taken by whichever sweeps first; the other sees nothing
        # to do even though the queue was non-empty when it started.
        assert first.sweep() == 1
        q.add(settlement(reference="C"))
        assert second.sweep() == 0


class TestFlowQueuesPendingTransfers:
    """The join between a pending eko response and the poller that follows it."""

    def flow(self, result, settlements=None):
        from whatsapp_voice.agent.payment_flow import PaymentFlow
        from whatsapp_voice.agent.payments import (
            Payee,
            PendingIntents,
            prepare_intent,
        )
        from whatsapp_voice.config import PaymentSettings

        class Gateway:
            is_stub = False

            def confirm_and_execute(self, intent, pin):
                return result

        pending = PendingIntents()
        intent = prepare_intent(
            "910000000001", Payee(phone="9000000000"), 1000, ttl_s=180
        )
        pending.put(intent)
        flow = PaymentFlow(
            Gateway(), pending, PaymentSettings(enabled=True), settlements=settlements
        )
        return flow, intent

    def test_a_pending_transfer_is_queued_for_polling(self, queue):
        flow, intent = self.flow(
            TransferResult(ok=False, pending=True, reference="TXN00001"), queue
        )
        outcome = flow.handle("910000000001", "1234")

        [queued] = queue.all()
        assert queued.reference == "TXN00001"
        assert queued.amount_paise == 1000
        assert "bata dunga" in outcome.reply

    def test_without_a_queue_it_does_not_promise_a_follow_up(self):
        """A stub deployment has nothing to poll. Saying "I'll tell you" there
        would be a promise nothing can keep."""
        flow, _ = self.flow(
            TransferResult(ok=False, pending=True, reference="TXN00001"), None
        )
        outcome = flow.handle("910000000001", "1234")

        assert "bata dunga" not in outcome.reply
        assert "statement check" in outcome.reply

    def test_a_settled_transfer_is_not_queued(self, queue):
        flow, _ = self.flow(TransferResult(ok=True, reference="TXN00001"), queue)
        flow.handle("910000000001", "1234")

        assert queue.all() == []

    def test_a_wrong_pin_is_not_queued(self, queue):
        flow, _ = self.flow(
            TransferResult(ok=False, pin_rejected=True, message="PIN galat hai."), queue
        )
        flow.handle("910000000001", "1234")

        assert queue.all() == []

    def test_a_broken_queue_does_not_break_the_reply(self):
        """Bookkeeping must never cost the payer their answer."""
        class Exploding:
            def add(self, _):
                raise RuntimeError("store down")

        flow, _ = self.flow(
            TransferResult(ok=False, pending=True, reference="TXN00001"), Exploding()
        )
        outcome = flow.handle("910000000001", "1234")

        assert "TXN00001" in outcome.reply
        assert "bata dunga" not in outcome.reply


class TestItKeepsTrackingUntilItKnows:
    """A pending transfer is followed until eko answers.

    There is deliberately no small attempt cap. An initiated transfer cannot be
    recalled — eko has no cancel endpoint, and its refund API covers
    already-failed cash transactions with customer OTP, not money in flight —
    so giving up early would not stop the transfer, only stop us watching it.
    The payer would be left with "not confirmed" as the last word on their
    money.
    """

    def pending(self, times: int = 20):
        return FakeGateway(*[TransferResult(ok=False, pending=True)] * times)

    def poller(self, queue, gateway, client):
        return SettlementPoller(
            queue, gateway, client, interval_s=60.0, give_up_after_s=SIX_HOURS
        )

    def test_it_is_still_queued_after_many_pending_answers(self, queue):
        queue.add(settlement())
        client = FakeClient()
        poller = self.poller(queue, self.pending(), client)

        for _ in range(10):
            assert poller.sweep() == 0

        assert len(queue.all()) == 1, "still being followed"
        assert client.sent == [], "and the payer is not being pestered"

    def test_the_attempt_count_climbs_without_closing_it(self, queue):
        queue.add(settlement())
        poller = self.poller(queue, self.pending(), FakeClient())
        for _ in range(5):
            poller.sweep()

        assert queue.all()[0].attempts == 5

    def test_it_resolves_the_moment_eko_answers(self, queue):
        """Four pending answers, then a success — the payer hears about it."""
        queue.add(settlement())
        client = FakeClient()
        gw = FakeGateway(
            *[TransferResult(ok=False, pending=True)] * 4,
            TransferResult(ok=True, reference="TXN00001"),
        )
        poller = self.poller(queue, gw, client)

        for _ in range(4):
            assert poller.sweep() == 0
        assert poller.sweep() == 1

        [(_, body)] = client.sent
        assert "Transaction successful" in body
        assert queue.all() == []

    def test_a_long_eko_outage_does_not_close_the_transfer(self, queue):
        """Unreachable says nothing about where the money is."""
        queue.add(settlement())
        poller = self.poller(queue, FakeGateway(*[None] * 10), FakeClient())
        for _ in range(10):
            poller.sweep()

        assert len(queue.all()) == 1

    def test_the_age_backstop_reports_unknown_not_failed(self, queue):
        """eko never said the transfer failed, so the audit must not either."""
        queue.add(settlement(created_at=0.0))
        client = FakeClient()
        poller = self.poller(queue, self.pending(), client)

        assert poller.sweep() == 1
        [(_, body)] = client.sent
        assert "track nahi kar paunga" in body
        assert "fail" not in body.lower()


class TestTheSuccessMessageReplacesTheEarlierDoubt:
    def test_it_says_successful_in_as_many_words(self):
        gw = FakeGateway(TransferResult(ok=True, reference="TXN00001"))
        outcome = poll_once(settlement(), gw, SIX_HOURS)

        assert "Transaction successful" in outcome.message
        assert "10.00" in outcome.message
        assert "TXN00001" in outcome.message

    def test_a_reversal_still_says_the_money_comes_back(self):
        gw = FakeGateway(TransferResult(ok=False, message="Refunded"))
        outcome = poll_once(settlement(), gw, SIX_HOURS)

        assert outcome.settled is False
        assert "wapas aa jayega" in outcome.message
