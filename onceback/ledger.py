"""Per-payment refund ledger: the duplicate lock that keeps every refund to once."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

NS = uuid.UUID("6f1d3a52-2b8e-4c51-9a0e-0bacc0ffee01")


def request_id(*parts: str) -> str:
    """Stable request_id: same operation -> same id (safe retry); new operation -> new id."""
    return str(uuid.uuid5(NS, "|".join(parts)))


class LedgerError(Exception):
    pass


@dataclass
class PaymentLedger:
    payment_intent_id: str
    currency: str
    captured: float
    refunded: float = 0.0                       # completed refunds + payouts
    in_flight: dict[str, float] = field(default_factory=dict)   # hold_id -> amount
    disputed: float = 0.0                       # amount clawed back via chargeback
    events: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)   # Airwallex refund / transfer ids that returned money

    def refundable(self) -> float:
        return round(self.captured - self.refunded - sum(self.in_flight.values()) - self.disputed, 2)


class Ledger:
    def __init__(self) -> None:
        self._p: dict[str, PaymentLedger] = {}
        self._lock = threading.Lock()

    def open(self, payment_intent_id: str, currency: str, captured: float) -> PaymentLedger:
        with self._lock:
            return self._p.setdefault(payment_intent_id, PaymentLedger(payment_intent_id, currency, captured))

    def get(self, pid: str) -> PaymentLedger:
        return self._p[pid]

    def state(self, pid: str) -> str | None:
        p = self._p[pid]
        if p.in_flight:
            return "IN_FLIGHT"
        if p.refunded > 0:
            return "DONE"
        return None

    def hold(self, pid: str, hold_id: str, amount: float) -> None:
        with self._lock:
            p = self._p[pid]
            if hold_id in p.in_flight:
                return  # idempotent: this exact operation already holds the money
            if p.in_flight:
                raise LedgerError(f"another refund is already in flight on {pid}")
            if amount > p.refundable() + 1e-9:
                raise LedgerError(f"hold {amount:,.2f} exceeds refundable {p.refundable():,.2f} on {pid}")
            p.in_flight[hold_id] = amount
            p.events.append(f"hold {hold_id} {amount}")

    def commit(self, pid: str, hold_id: str, ref: str = "") -> None:
        with self._lock:
            p = self._p[pid]
            amt = p.in_flight.pop(hold_id)
            p.refunded = round(p.refunded + amt, 2)
            if ref:
                p.refs.append(ref)
            p.events.append(f"commit {hold_id} {amt} {ref}".strip())

    def release(self, pid: str, hold_id: str) -> None:
        with self._lock:
            p = self._p[pid]
            amt = p.in_flight.pop(hold_id, 0.0)
            p.events.append(f"release {hold_id} {amt}")

    def chargeback(self, pid: str, amount: float) -> None:
        with self._lock:
            p = self._p[pid]
            p.disputed = round(p.disputed + amount, 2)
            p.events.append(f"chargeback {amount}")

    def total_returned(self, pid: str) -> float:
        p = self._p[pid]
        return round(p.refunded + p.disputed, 2)
