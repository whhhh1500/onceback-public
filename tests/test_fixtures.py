"""Tests against responses recorded from the real Airwallex sandbox (2026-10-10).

probe_*.jsonl: one labelled call per behaviour we depend on.
live_run_*.jsonl: every API call of a full `python -m onceback --live` run.
"""
from onceback.agent import OnceBack
from onceback.airwallex import Airwallex
from onceback.demo import BANK, ORDERS, REQUESTS, TODAY
from onceback.mock import name_match
from onceback.models import Action, Decision, Order
from onceback.policy import decide_payout

from replay import ReplayTransport, load

PROBE = load("probe_2026-10-10.jsonl")
P = {e["label"]: e for e in PROBE}


def test_probe_fixture_has_every_behaviour_and_no_secrets():
    want = {"confirm_native_api_disabled", "refund_blocked_open_dispute", "refund_blocked_accepted_dispute",
            "refund_duplicate_request", "verify_jp_unsupported", "verify_gb_full", "verify_gb_partial",
            "verify_gb_not_matched", "jp_local_missing_bank_account_category", "sim_transfer_processing_500",
            "fx_quote", "fx_conversion", "dispute_challenge", "dispute_accept"}
    assert want <= set(P)
    blob = str(PROBE)
    assert "eyJ" not in blob and "x-api-key" not in blob.lower()
    # server-side card confirm is disabled for this account -> payments go through Airwallex.js
    e = P["confirm_native_api_disabled"]
    assert e["status"] == 400 and "native API" in e["response"]["message"]


def test_refund_on_disputed_payment_answers_amount_above_limit():
    for label in ("refund_blocked_open_dispute", "refund_blocked_accepted_dispute"):
        e = P[label]
        assert e["status"] == 400 and e["response"]["code"] == "amount_above_limit"
        assert "limit 0.00" in e["response"]["message"]
    from onceback.agent import REFUND_BLOCKED_CODES
    assert "amount_above_limit" in REFUND_BLOCKED_CODES


def test_replayed_request_id_is_rejected_not_echoed():
    e = P["refund_duplicate_request"]
    assert e["status"] == 400 and e["response"]["code"] == "duplicate_request"


def test_jp_has_no_name_check_and_policy_escalates_to_a_person():
    e = P["verify_jp_unsupported"]
    assert e["status"] == 400 and e["response"]["code"] == "transfer_method_unsupported"
    from onceback.agent import VERIFY_UNSUPPORTED_CODES
    assert e["response"]["code"] in VERIFY_UNSUPPORTED_CODES
    base = Decision(Action.REQUEST_BANK_DETAILS, 48000, "JPY", [], True, {"amount": 48000})
    d = decide_payout("CANNOT_VERIFY", None, base)
    assert d.action == Action.PAYOUT_BANK and d.needs_approval


def test_gb_name_check_values_and_mock_agree():
    holder = "Betty Morris"
    for label, want in (("verify_gb_full", "FULL_MATCH"), ("verify_gb_partial", "PARTIAL_MATCH"),
                        ("verify_gb_not_matched", "NOT_MATCHED")):
        e = P[label]
        got = e["response"]["details"]["account_name_match_result"]
        assert e["response"]["code"] == "VERIFIED" and got == want
        assert name_match(e["json"]["bank_details"]["account_name"], holder) == want


def test_jp_local_beneficiary_needs_bank_account_category():
    e = P["jp_local_missing_bank_account_category"]
    assert e["status"] == 400 and e["response"]["code"] == "validation_failed"
    assert e["response"]["details"]["errors"][0]["source"].endswith("bank_account_category")
    assert BANK["C2"].bank_account_category in ("Savings", "Checking")


def test_transfer_simulation_rejects_processing():
    e = P["sim_transfer_processing_500"]
    assert e["status"] == 500 and e["json"]["next_status"] == "PROCESSING"


def test_fx_quote_rate_is_buy_units_per_sell_unit_and_conversion_settles():
    q, c = P["fx_quote"]["response"], P["fx_conversion"]["response"]
    assert q["sell_currency"] == "USD" and q["client_rate"] > 1     # USDJPY ~157, not 0.0064
    assert abs(q["buy_amount"] / q["client_rate"] - q["sell_amount"]) < 0.05
    assert c["status"] == "SETTLED" and c["quote_id"] == q["quote_id"]


def test_dispute_accept_and_challenge_shapes():
    acc = P["dispute_accept"]["response"]
    ch = P["dispute_challenge"]["response"]
    assert acc["status"] == "ACCEPTED" and acc["accept_details"][0]["reason"] == "VALID_CUSTOMER_DISPUTE"
    assert ch["status"] == "CHALLENGED" and ch["reason"]["type"] == "CREDIT_NOT_PROCESSED"


def test_replay_full_live_run():
    """Re-runs the agent on the exact sandbox responses of the recorded live run.
    Asserts the agent makes the same calls with the same bodies and reaches the same outcomes."""
    rec = load("live_run_2026-10-10.jsonl")
    creates = [e for e in rec if e["path"] == "/pa/payment_intents/create"]
    start = next(i for i, e in enumerate(rec) if e["path"] == "/pa/payment_disputes")
    awx = Airwallex(ReplayTransport(rec[start:]))
    agent = OnceBack(awx, approver=lambda d, h: True, bank_details=lambda r, o: BANK.get(r.case_id),
                     poll_s=0.001)
    orders = {od["order_id"]: Order(payment_intent_id=c["response"]["id"], **od) for od, c in zip(ORDERS, creates)}
    agent.hooks["before_refund"] = lambda o: (
        awx.sim.dispute(o.payment_intent_id, o.amount, "13.1", comment="item not received")
        if o.order_id == "ORD-1003" else None)
    outs = {r.case_id: agent.handle(orders[r.order_id], r, TODAY).outcome for r in REQUESTS}
    o4 = orders["ORD-1004"]
    awx.sim.dispute(o4.payment_intent_id, o4.amount, "13.6", comment="credit not processed")
    outs["C4-cb"] = agent.on_dispute(o4, "C4-chargeback").outcome
    assert outs == {"C1": "REFUNDED_TO_CARD", "C2": "REFUNDED_TO_BANK", "C3": "ACCEPT_DISPUTE",
                    "C4": "REFUNDED_TO_CARD", "C1-dup": "DECLINE", "C5": "REFUNDED_TO_BANK",
                    "C4-cb": "CHALLENGE_DISPUTE"}
    for o in orders.values():
        assert agent.ledger.total_returned(o.payment_intent_id) <= o.amount
