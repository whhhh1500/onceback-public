from datetime import date

from onceback.agent import OnceBack
from onceback.airwallex import Airwallex
from onceback.demo import BANK, ORDERS, REGISTRY, REQUESTS, TEST_CARD, TODAY
from onceback.ledger import request_id
from onceback.mock import MockTransport
from onceback.models import BankDetails, Order, RefundRequest


def setup(approve=True, bank=None, registry=None):
    t = MockTransport(balances={"USD": 5000.0}, bank_registry=registry or REGISTRY)
    awx = Airwallex(t)
    seen = []
    agent = OnceBack(awx, approver=lambda d, h: seen.append((d.binding, h)) or approve,
                     bank_details=lambda r, o: (BANK if bank is None else bank).get(r.case_id))
    orders = {}
    for od in ORDERS:
        pi = awx.create_payment_intent(request_id("pi", od["order_id"]), od["amount"], od["currency"], od["order_id"])
        awx.confirm_payment_intent(pi["id"], request_id("cf", od["order_id"]), TEST_CARD)
        orders[od["order_id"]] = Order(payment_intent_id=pi["id"], **od)
    return t, awx, agent, orders, seen


def R(case):
    return next(r for r in REQUESTS if r.case_id == case)


def test_full_demo_never_returns_more_than_captured():
    t, awx, agent, orders, _ = setup()
    agent.hooks["before_refund"] = lambda o: awx.sim.dispute(o.payment_intent_id, o.amount) \
        if o.order_id == "ORD-1003" else None
    outs = {r.case_id: agent.handle(orders[r.order_id], r, TODAY).outcome for r in REQUESTS}
    awx.sim.dispute(orders["ORD-1004"].payment_intent_id, 640)
    outs["C4-cb"] = agent.on_dispute(orders["ORD-1004"], "C4-cb").outcome
    assert outs == {"C1": "REFUNDED_TO_CARD", "C2": "REFUNDED_TO_BANK", "C3": "ACCEPT_DISPUTE",
                    "C4": "REFUNDED_TO_CARD", "C1-dup": "DECLINE", "C5": "REFUNDED_TO_BANK",
                    "C4-cb": "CHALLENGE_DISPUTE"}
    for o in orders.values():
        assert agent.ledger.total_returned(o.payment_intent_id) <= o.amount
    # no refund call was ever made for the chargebacked payment
    assert not [r for r in t.refunds.values() if r["payment_intent_id"] == orders["ORD-1003"].payment_intent_id]


def test_injected_amount_never_reaches_the_api():
    t, awx, agent, orders, seen = setup()
    agent.handle(orders["ORD-1004"], R("C4"), TODAY)
    assert [r["amount"] for r in t.refunds.values()] == [576.0]
    assert seen and seen[0][0]["amount"] == 576.0


def test_rejected_approval_moves_nothing():
    t, awx, agent, orders, seen = setup(approve=False)
    out = agent.handle(orders["ORD-1004"], R("C4"), TODAY).outcome
    assert out == "AWAITING_APPROVAL" and not t.refunds and len(seen) == 1


def test_chargeback_race_accepts_instead_of_refunding():
    t, awx, agent, orders, _ = setup()
    agent.hooks["before_refund"] = lambda o: awx.sim.dispute(o.payment_intent_id, o.amount)
    tr = agent.handle(orders["ORD-1003"], R("C3"), TODAY)
    assert tr.outcome == "ACCEPT_DISPUTE" and not t.refunds
    assert [d["status"] for d in t.disputes.values()] == ["ACCEPTED"]
    assert agent.ledger.total_returned(orders["ORD-1003"].payment_intent_id) == 89.0


def test_name_mismatch_stops_payout():
    # account belongs to someone other than the buyer -> no payout, no approval prompt
    t, awx, agent, orders, seen = setup(registry={"040568:10006132": "Harry Sato"})
    out = agent.handle(orders["ORD-1005"], R("C5"), TODAY).outcome
    assert out == "ESCALATE" and not t.transfers and not seen


def test_name_check_is_against_buyer_not_typed_name():
    # customer typed the right holder name, but the BUYER on the order is checked
    sent = []
    t, awx, agent, orders, _ = setup()
    orig = t.request
    t.request = lambda m, p, json=None, params=None: (sent.append(json) if p.endswith("verify_account") else None) \
        or orig(m, p, json, params)
    agent.handle(orders["ORD-1005"], R("C5"), TODAY)
    assert sent[0]["bank_details"]["account_name"] == "Betty Morriss"


def test_partial_match_needs_approval_bound_to_account():
    t, awx, agent, orders, seen = setup(approve=False)
    out = agent.handle(orders["ORD-1005"], R("C5"), TODAY).outcome
    assert out == "AWAITING_APPROVAL" and not t.transfers
    assert seen[0][0]["name_check"] == "PARTIAL_MATCH" and seen[0][0]["beneficiary_account"]


def test_missing_bank_details_moves_nothing():
    t, awx, agent, orders, _ = setup(bank={})
    out = agent.handle(orders["ORD-1002"], R("C2"), TODAY).outcome
    assert out == "AWAITING_BANK_DETAILS" and not t.transfers


def test_payout_funds_currency_with_fx_once():
    t, awx, agent, orders, _ = setup()
    agent.handle(orders["ORD-1002"], R("C2"), TODAY)
    agent.handle(orders["ORD-1002"], RefundRequest("C2b", "ORD-1002", R("C2").message, date(2026, 10, 10)), TODAY)
    assert len(t.transfers) == 1 and sum(q["used"] for q in t.quotes.values()) == 1


def test_existing_open_dispute_is_accepted_not_refunded():
    t, awx, agent, orders, _ = setup()
    awx.sim.dispute(orders["ORD-1001"].payment_intent_id, 120)
    out = agent.handle(orders["ORD-1001"], R("C1"), TODAY).outcome
    assert out == "ACCEPT_DISPUTE" and not t.refunds


def test_lost_refund_response_is_adopted_not_resent():
    # first attempt reached Airwallex but the response was lost (crash before commit)
    t, awx, agent, orders, _ = setup()
    o = orders["ORD-1001"]
    real_create = awx.create_refund
    calls = []

    def lossy(*a, **k):
        calls.append(a)
        real_create(*a, **k)
        if len(calls) == 1:
            raise TimeoutError("response lost")
        raise AssertionError("unreachable")
    awx.create_refund = lossy
    import pytest
    with pytest.raises(TimeoutError):
        agent.handle(o, R("C1"), TODAY)
    # process restarts with a fresh ledger; same deterministic request_id
    agent2 = OnceBack(awx, approver=lambda d, h: True)
    awx.create_refund = real_create
    tr = agent2.handle(o, R("C1"), TODAY)
    assert tr.outcome == "REFUNDED_TO_CARD" and len(t.refunds) == 1
    assert any("adopting" in line for _, line in tr.lines)
