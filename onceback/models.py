from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import Any


class Intent(str, Enum):
    DAMAGED = "damaged"
    NOT_RECEIVED = "not_received"
    CHANGED_MIND = "changed_mind"
    WRONG_ITEM = "wrong_item"
    OTHER = "other"


class Action(str, Enum):
    REFUND_CARD = "REFUND_CARD"            # POST /pa/refunds/create
    PAYOUT_BANK = "PAYOUT_BANK"            # verify_account -> beneficiary -> (FX) -> transfer
    REQUEST_BANK_DETAILS = "REQUEST_BANK_DETAILS"
    ACCEPT_DISPUTE = "ACCEPT_DISPUTE"      # chargeback already returns the money: refunding too = paying twice
    CHALLENGE_DISPUTE = "CHALLENGE_DISPUTE"  # refund already went out: challenge with "credit processed"
    DECLINE = "DECLINE"
    ESCALATE = "ESCALATE"


@dataclass
class Order:
    order_id: str
    payment_intent_id: str
    currency: str
    amount: float
    paid_on: date
    customer_name: str
    customer_country: str
    delivery_status: str = "DELIVERED"     # DELIVERED | IN_TRANSIT | LOST | UNKNOWN
    delivery_signed: bool = False
    prior_refunds_90d: int = 0
    address: dict[str, str] = field(default_factory=dict)   # street_address, city, postcode


@dataclass
class RefundRequest:
    case_id: str
    order_id: str
    message: str
    received_on: date
    attachments: list[str] = field(default_factory=list)


@dataclass
class Reading:
    """What the model is allowed to return. Never an amount the code will use."""
    intent: Intent
    evidence: list[str]
    injection_suspected: bool
    summary: str
    citations: list[str] = field(default_factory=list)
    claimed_amount: float | None = None   # informational only, never used for money
    source: str = "heuristic"


@dataclass
class BankDetails:
    account_name: str
    bank_country_code: str
    account_number: str | None = None
    iban: str | None = None
    account_routing_type1: str | None = None
    account_routing_value1: str | None = None
    account_routing_type2: str | None = None
    account_routing_value2: str | None = None
    swift_code: str | None = None
    local_clearing_system: str | None = None
    bank_account_category: str | None = None   # JP LOCAL requires Savings/Checking
    bank_name: str | None = None

    VERIFY_FIELDS = ("account_name", "account_number", "iban", "account_routing_type1",
                     "account_routing_value1", "bank_country_code", "swift_code", "local_clearing_system")

    def for_verify(self, name: str) -> dict[str, Any]:
        d = {k: v for k, v in self.as_api().items() if k in self.VERIFY_FIELDS}
        d["account_name"] = name
        return d

    def as_api(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Decision:
    action: Action
    amount: float = 0.0
    currency: str = ""
    reasons: list[str] = field(default_factory=list)
    needs_approval: bool = False
    binding: dict[str, Any] = field(default_factory=dict)

    def explain(self) -> str:
        amt = f" {self.amount:,.2f} {self.currency}" if self.amount else ""
        gate = " [needs human approval]" if self.needs_approval else ""
        return f"{self.action.value}{amt}{gate}: " + "; ".join(self.reasons)
