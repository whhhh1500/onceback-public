"""Refund policy in code. The model never sets amounts, rails or limits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from .models import Action, Decision, Intent, Order, Reading, RefundRequest

# Indicative USD value of one unit of each currency (used only for limits,
# never for booking money; bookings use a fresh Airwallex FX quote).
DEFAULT_USD_PER_UNIT = {"USD": 1.0, "EUR": 1.087, "GBP": 1.266, "JPY": 0.00667, "AUD": 0.66, "SGD": 0.74}


@dataclass
class Policy:
    return_window_days: int = 30
    card_refund_max_age_days: int = 180      # beyond this the card rail is treated as closed
    restocking_fee_pct: float = 10.0         # changed-mind returns only
    auto_approve_limit_usd: float = 150.0
    abuse_refunds_90d: int = 3
    usd_per_unit: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_USD_PER_UNIT))

    def to_usd(self, amount: float, currency: str) -> float:
        return round(amount * self.usd_per_unit[currency], 2)


def binding_hash(binding: dict) -> str:
    """Approval is bound to exactly these terms; any change invalidates it."""
    blob = json.dumps(binding, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def refund_amount(order: Order, reading: Reading, policy: Policy) -> tuple[float, list[str]]:
    """Amount owed to the customer, computed only from the order and policy."""
    reasons: list[str] = []
    amt = order.amount
    if reading.intent == Intent.CHANGED_MIND:
        fee = round(order.amount * policy.restocking_fee_pct / 100, 2)
        amt = round(order.amount - fee, 2)
        reasons.append(f"changed-mind return: {policy.restocking_fee_pct:g}% restocking fee ({fee:,.2f} {order.currency})")
    else:
        reasons.append(f"{reading.intent.value}: full order value")
    if reading.claimed_amount is not None and abs(reading.claimed_amount - amt) > 0.005:
        reasons.append(f"customer asked for {reading.claimed_amount:,.2f}; ignored, amount comes from the order")
    return amt, reasons


def choose_rail(order: Order, today: date, policy: Policy) -> tuple[Action, str]:
    """Card refund while the card rail is open; otherwise a bank payout."""
    age = (today - order.paid_on).days
    if age > policy.card_refund_max_age_days:
        return Action.REQUEST_BANK_DETAILS, (f"payment is {age}d old, past the {policy.card_refund_max_age_days}d "
                                             "card refund limit; refund must go out as a bank payout")
    return Action.REFUND_CARD, f"payment is {age}d old, card refund rail open (<= {policy.card_refund_max_age_days}d)"


def decide(order: Order, req: RefundRequest, reading: Reading, policy: Policy, today: date,
           refundable: float) -> Decision:
    """What is owed, on which rail, and whether a person has to approve. Pure code."""
    reasons: list[str] = []
    if reading.injection_suspected:
        reasons.append("message contains instructions aimed at the agent; treated as data, flagged for review")
    if reading.intent == Intent.DAMAGED and not req.attachments:
        return Decision(Action.ESCALATE, 0, order.currency, reasons + ["damage claim without a photo"], True)
    if reading.intent == Intent.NOT_RECEIVED and order.delivery_signed:
        return Decision(Action.ESCALATE, 0, order.currency,
                        reasons + ["carrier shows a signed delivery; needs a person"], True)
    if reading.intent == Intent.CHANGED_MIND and (req.received_on - order.paid_on).days > policy.return_window_days:
        return Decision(Action.DECLINE, 0, order.currency,
                        reasons + [f"change of mind outside the {policy.return_window_days}-day return window"])
    if reading.intent == Intent.OTHER:
        return Decision(Action.ESCALATE, 0, order.currency, reasons + ["could not classify request"], True)

    if refundable <= 0.005:
        return Decision(Action.DECLINE, 0, order.currency,
                        reasons + ["nothing left to refund on this payment; it was already returned in full"])

    amount, amt_reasons = refund_amount(order, reading, policy)
    reasons += amt_reasons
    if amount > refundable + 1e-9:
        return Decision(Action.ESCALATE, 0, order.currency,
                        reasons + [f"owed {amount:,.2f} exceeds refundable remainder {refundable:,.2f}"], True)

    rail, rail_reason = choose_rail(order, today, policy)
    reasons.append(rail_reason)

    usd = policy.to_usd(amount, order.currency)
    needs_approval = False
    if usd > policy.auto_approve_limit_usd:
        needs_approval = True
        reasons.append(f"~USD {usd:,.2f} above auto-approve limit USD {policy.auto_approve_limit_usd:,.0f}")
    if order.prior_refunds_90d >= policy.abuse_refunds_90d:
        needs_approval = True
        reasons.append(f"customer has {order.prior_refunds_90d} refunds in 90d")
    if reading.injection_suspected:
        needs_approval = True

    binding = {"order_id": order.order_id, "payment_intent_id": order.payment_intent_id,
               "amount": amount, "currency": order.currency, "rail": rail.value}
    return Decision(rail, amount, order.currency, reasons, needs_approval, binding)


def decide_on_dispute(refund_state: str | None, dispute_amount: float, owed: float, currency: str) -> Decision:
    """A chargeback appeared on a payment we are refunding (or refunded).

    refund_state: None (not sent), 'IN_FLIGHT', 'DONE'
    """
    if refund_state is None:
        return Decision(Action.ACCEPT_DISPUTE, dispute_amount, currency, [
            "chargeback opened before our refund went out",
            "accepting returns the money through the scheme; refunding as well would pay twice",
            "refund cancelled",
        ])
    if refund_state == "IN_FLIGHT":
        return Decision(Action.ESCALATE, 0, currency, [
            "chargeback opened while a refund is in flight; wait for refund outcome before acting",
        ], True)
    return Decision(Action.CHALLENGE_DISPUTE, dispute_amount, currency, [
        f"refund of {owed:,.2f} {currency} already completed",
        "challenge with 'credit processed' evidence instead of accepting a second debit",
    ])


VERIFY_OUTCOMES = {  # real sandbox enum: FULL_MATCH | PARTIAL_MATCH | NOT_MATCHED
    "FULL_MATCH": (True, False, "account verified, holder name matches the buyer"),
    "PARTIAL_MATCH": (True, True, "account verified, holder name only partially matches the buyer"),
    "NOT_MATCHED": (False, True, "account holder name does not match the buyer; refusing to pay a third party"),
    "NO_MATCH": (False, True, "account holder name does not match the buyer; refusing to pay a third party"),
}


def decide_payout(verify_code: str, match: str | None, base: Decision) -> Decision:
    """After verify_account: proceed, gate, or stop the bank payout."""
    reasons = list(base.reasons)
    if verify_code in ("CANNOT_VERIFY", "EXTERNAL_SERVICE_UNAVAILABLE"):
        reasons.append(f"name check unavailable for this corridor ({verify_code}); a person must confirm the payee")
        return Decision(Action.PAYOUT_BANK, base.amount, base.currency, reasons, True,
                        dict(base.binding, rail=Action.PAYOUT_BANK.value, name_check=verify_code))
    if verify_code != "VERIFIED":
        return Decision(Action.REQUEST_BANK_DETAILS, 0, base.currency,
                        reasons + [f"bank account verification returned {verify_code}; ask customer again"])
    ok, gate, why = VERIFY_OUTCOMES.get(match or "", (False, True, f"unknown name-match result {match}"))
    reasons.append(why)
    if not ok:
        return Decision(Action.ESCALATE, 0, base.currency, reasons, True)
    d = Decision(Action.PAYOUT_BANK, base.amount, base.currency, reasons,
                 base.needs_approval or gate, dict(base.binding, rail=Action.PAYOUT_BANK.value, name_check=match))
    return d
