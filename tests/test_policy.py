from datetime import date

from onceback.models import Action, Intent, Order, Reading, RefundRequest
from onceback.policy import Policy, binding_hash, decide, decide_on_dispute, decide_payout

TODAY = date(2026, 10, 10)


def order(**kw):
    base = dict(order_id="O1", payment_intent_id="int_1", currency="EUR", amount=100.0,
                paid_on=date(2026, 10, 1), customer_name="A B", customer_country="FR")
    base.update(kw)
    return Order(**base)


def reading(intent, inj=False, claimed=None):
    return Reading(intent=intent, evidence=[], injection_suspected=inj, summary="", claimed_amount=claimed)


def req(att=None, on=date(2026, 10, 9)):
    return RefundRequest("C", "O1", "msg", on, att or [])


def test_small_damaged_with_photo_auto_card_refund():
    d = decide(order(), req(["p.jpg"]), reading(Intent.DAMAGED), Policy(), TODAY, 100.0)
    assert d.action == Action.REFUND_CARD and d.amount == 100.0 and not d.needs_approval


def test_damaged_without_photo_escalates():
    d = decide(order(), req(), reading(Intent.DAMAGED), Policy(), TODAY, 100.0)
    assert d.action == Action.ESCALATE and d.needs_approval


def test_changed_mind_restocking_fee_and_claim_ignored():
    d = decide(order(amount=640.0, currency="GBP"), req(), reading(Intent.CHANGED_MIND, claimed=6400), Policy(),
               TODAY, 640.0)
    assert d.amount == 576.0
    assert any("ignored" in r for r in d.reasons)
    assert d.needs_approval  # ~USD 729 > 150


def test_injection_forces_human_even_when_small():
    d = decide(order(amount=20.0), req(["p"]), reading(Intent.DAMAGED, inj=True), Policy(), TODAY, 20.0)
    assert d.action == Action.REFUND_CARD and d.needs_approval and d.amount == 20.0


def test_old_payment_moves_to_bank_rail():
    d = decide(order(paid_on=date(2026, 3, 10), currency="JPY", amount=48000), req(), reading(Intent.WRONG_ITEM),
               Policy(), TODAY, 48000)
    assert d.action == Action.REQUEST_BANK_DETAILS and d.amount == 48000
    assert any("214d old" in r for r in d.reasons)


def test_changed_mind_outside_return_window_declines():
    d = decide(order(paid_on=date(2026, 8, 1)), req(), reading(Intent.CHANGED_MIND), Policy(), TODAY, 100.0)
    assert d.action == Action.DECLINE and d.amount == 0


def test_repeat_refunder_needs_approval():
    d = decide(order(amount=30.0, prior_refunds_90d=3), req(["p"]), reading(Intent.DAMAGED), Policy(), TODAY, 30.0)
    assert d.action == Action.REFUND_CARD and d.needs_approval


def test_nothing_left_to_refund_declines():
    d = decide(order(), req(["p"]), reading(Intent.DAMAGED), Policy(), TODAY, 0.0)
    assert d.action == Action.DECLINE


def test_owed_more_than_refundable_escalates():
    d = decide(order(), req(["p"]), reading(Intent.DAMAGED), Policy(), TODAY, 40.0)
    assert d.action == Action.ESCALATE and d.needs_approval


def test_binding_hash_changes_with_any_term():
    b = {"order_id": "O1", "amount": 576.0, "currency": "GBP", "rail": "REFUND_CARD"}
    h = binding_hash(b)
    assert h == binding_hash(dict(b))
    assert h != binding_hash(dict(b, amount=6400.0))
    assert h != binding_hash(dict(b, beneficiary_account="x"))


def test_dispute_before_refund_accepts():
    d = decide_on_dispute(None, 89.0, 0.0, "USD")
    assert d.action == Action.ACCEPT_DISPUTE


def test_dispute_while_refund_in_flight_waits():
    d = decide_on_dispute("IN_FLIGHT", 89.0, 0.0, "USD")
    assert d.action == Action.ESCALATE and d.needs_approval


def test_dispute_after_refund_challenges():
    d = decide_on_dispute("DONE", 640.0, 576.0, "GBP")
    assert d.action == Action.CHALLENGE_DISPUTE and "576.00" in d.explain()


def test_payout_name_check_outcomes():
    base = decide(order(paid_on=date(2026, 3, 10)), req(), reading(Intent.WRONG_ITEM), Policy(), TODAY, 100.0)
    assert decide_payout("VERIFIED", "FULL_MATCH", base).action == Action.PAYOUT_BANK
    pm = decide_payout("VERIFIED", "PARTIAL_MATCH", base)
    assert pm.action == Action.PAYOUT_BANK and pm.needs_approval
    assert decide_payout("VERIFIED", "NOT_MATCHED", base).action == Action.ESCALATE
    cv = decide_payout("CANNOT_VERIFY", None, base)
    assert cv.action == Action.PAYOUT_BANK and cv.needs_approval and cv.binding["name_check"] == "CANNOT_VERIFY"
    assert decide_payout("NOT_VERIFIED", None, base).action == Action.REQUEST_BANK_DETAILS
