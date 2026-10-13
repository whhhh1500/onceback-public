import threading

import pytest

from onceback.airwallex import Airwallex, AirwallexError
from onceback.ledger import Ledger, LedgerError, request_id
from onceback.mock import MockTransport, name_match


def test_request_id_is_stable_per_operation():
    assert request_id("refund", "int_1", "C1") == request_id("refund", "int_1", "C1")
    assert request_id("refund", "int_1", "C1") != request_id("refund", "int_1", "C2")


def test_hold_never_exceeds_captured():
    led = Ledger(); led.open("p", "EUR", 100.0)
    led.hold("p", "h1", 60.0); led.commit("p", "h1")
    with pytest.raises(LedgerError):
        led.hold("p", "h2", 50.0)
    led.hold("p", "h3", 40.0); led.commit("p", "h3")
    assert led.total_returned("p") == 100.0 and led.get("p").refundable() == 0


def test_chargeback_counts_as_money_returned():
    led = Ledger(); led.open("p", "USD", 89.0)
    led.chargeback("p", 89.0)
    assert led.total_returned("p") == 89.0
    with pytest.raises(LedgerError):
        led.hold("p", "h", 1.0)


def test_concurrent_refunds_only_one_wins():
    led = Ledger(); led.open("p", "EUR", 100.0)
    wins, errs = [], []

    def go(i):
        try:
            led.hold("p", f"h{i}", 100.0); wins.append(i)
        except LedgerError:
            errs.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1 and len(errs) == 19


def _paid(awx, amount=100.0, cur="EUR"):
    pi = awx.create_payment_intent(request_id("pi", str(amount), cur), amount, cur, "O")
    awx.confirm_payment_intent(pi["id"], request_id("c", pi["id"]), {})
    return pi["id"]


def test_mock_refund_blocked_during_dispute():
    t = MockTransport(); awx = Airwallex(t)
    pid = _paid(awx)
    awx.sim.dispute(pid, 100)
    with pytest.raises(AirwallexError) as e:
        awx.create_refund(request_id("r"), pid, 100, "x")
    assert e.value.code == "amount_above_limit" and "limit 0.00" in e.value.message


def test_mock_refund_request_id_reuse_rejected():
    t = MockTransport(); awx = Airwallex(t)
    pid = _paid(awx)
    rid = request_id("r1")
    a = awx.create_refund(rid, pid, 40, "x")
    with pytest.raises(AirwallexError) as e:              # real sandbox: replay is rejected, not echoed
        awx.create_refund(rid, pid, 40, "x")
    assert e.value.code == "duplicate_request" and t.intents[pid]["refunded"] == 40
    assert awx.find_refund(pid, rid)["id"] == a["id"]
    with pytest.raises(AirwallexError) as e:
        awx.create_transfer(rid, "ben", 1, "USD", "r")   # same id, different operation
    assert e.value.code == "duplicate_request"


def test_mock_transfer_state_machine():
    t = MockTransport(balances={"USD": 100}); awx = Airwallex(t)
    ben = awx.create_beneficiary(request_id("b"), {"entity_type": "PERSONAL", "bank_details": {}}, ["LOCAL"])
    tr = awx.create_transfer(request_id("t"), ben["id"], 50, "USD", "ref")
    assert tr["status"] == "SCHEDULED"
    with pytest.raises(AirwallexError):
        awx.sim.transfer(tr["id"], "PROCESSING")        # sandbox rejects this transition (500)
    with pytest.raises(AirwallexError):
        awx.sim.transfer(tr["id"], "PAID")              # SENT before PAID
    awx.sim.transfer(tr["id"], "SENT")
    x = awx.sim.transfer(tr["id"], "FAILED")
    assert x["status"] == "CANCELLED" and t.balances["USD"] == 100


def test_name_match_mirrors_sandbox_answers():
    # observed live for GB 040568 / 10006132 (holder "Betty Morris")
    assert name_match("Betty Morris", "Betty Morris") == "FULL_MATCH"
    assert name_match("Betty Morriss", "Betty Morris") == "PARTIAL_MATCH"
    assert name_match("B Morris", "Betty Morris") == "NOT_MATCHED"
    assert name_match("Mrs Betty Morris", "Betty Morris") == "NOT_MATCHED"
