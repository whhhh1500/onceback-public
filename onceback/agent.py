"""Orchestration: read -> decide (code) -> approve (human, bound) -> act (Airwallex) -> reconcile."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .airwallex import Airwallex, AirwallexError
from .ledger import Ledger, LedgerError, request_id
from .models import Action, BankDetails, Decision, Order, RefundRequest
from .policy import Policy, binding_hash, decide, decide_on_dispute, decide_payout
from .reader import read

Approver = Callable[[Decision, str], bool]
BankDetailsSource = Callable[[RefundRequest, Order], BankDetails | None]

OPEN_DISPUTE = {"REQUIRES_RESPONSE", "CHALLENGED", "PENDING_REVIEW", "OPEN"}
NEEDS_RESPONSE = {"REQUIRES_RESPONSE"}
# Airwallex answers a refund on a disputed payment with amount_above_limit ("limit 0.00")
REFUND_BLOCKED_CODES = {"amount_above_limit", "invalid_status_for_operation"}
# verify_account has no name check for some corridors (JP: transfer_method_unsupported)
VERIFY_UNSUPPORTED_CODES = {"transfer_method_unsupported", "unsupported_country", "verification_not_supported"}


@dataclass
class Trace:
    case_id: str
    lines: list[tuple[str, str]] = field(default_factory=list)
    outcome: str = ""
    steps: list[dict] = field(default_factory=list)       # structured copy for the console
    listener: Callable[["Trace", dict], None] | None = None

    def add(self, stage: str, text: str, **data) -> None:
        self.lines.append((stage, text))
        step = {"stage": stage, "text": text, "at": time.time(), **({"data": data} if data else {})}
        self.steps.append(step)
        if self.listener:
            self.listener(self, step)

    def render(self) -> str:
        out = [f"== case {self.case_id} =="]
        out += [f"  [{s:<8}] {t}" for s, t in self.lines]
        out.append(f"  => {self.outcome}")
        return "\n".join(out)


def _decision(tr: "Trace", stage: str, d: Decision, **extra) -> None:
    tr.add(stage, d.explain(), action=d.action.value, amount=d.amount, currency=d.currency,
           needs_approval=d.needs_approval, reasons=d.reasons, **extra)


def _mask(bd: BankDetails) -> str:
    num = bd.iban or bd.account_number or ""
    route = bd.account_routing_value1 or bd.swift_code or ""
    return f"{bd.bank_country_code} {route} ****{num[-4:]}".strip()


def _account_fingerprint(bd: BankDetails) -> str:
    """Short stable id of the destination account; the approval is bound to it."""
    return hashlib.sha256(json.dumps(bd.as_api(), sort_keys=True).encode()).hexdigest()[:12]


class OnceBack:
    def __init__(self, awx: Airwallex, policy: Policy | None = None, ledger: Ledger | None = None,
                 approver: Approver | None = None, bank_details: BankDetailsSource | None = None,
                 use_llm: bool = False, home_currency: str = "USD", simulate_settlement: bool = True,
                 poll_s: float = 0.0, run_tag: str = ""):
        self.awx, self.policy = awx, policy or Policy()
        self.ledger = ledger or Ledger()
        self.approver: Approver = approver or (lambda d, h: False)
        self.bank_details: BankDetailsSource = bank_details or (lambda r, o: None)
        self.use_llm, self.home = use_llm, home_currency
        self.simulate_settlement, self.poll_s, self.run_tag = simulate_settlement, poll_s, run_tag
        self.hooks: dict[str, Callable[[Order], object]] = {}

    def rid(self, *parts: str) -> str:
        """Deterministic request_id per operation (scoped by run tag in live runs)."""
        return request_id(self.run_tag, *parts) if self.run_tag else request_id(*parts)

    # ------------------------------------------------------------------ refund requests
    def handle(self, order: Order, req: RefundRequest, today: date, tr: Trace | None = None) -> Trace:
        tr = tr or Trace(req.case_id)
        pid = order.payment_intent_id
        p = self.ledger.open(pid, order.currency, order.amount)
        tr.add("GOAL", f"resolve refund request on {order.order_id} ({order.amount:,.2f} {order.currency}, "
                       f"paid {order.paid_on}, customer {order.customer_name}, {order.customer_country}, "
                       f"payment {pid})", kind="goal", order_id=order.order_id, amount=order.amount,
               currency=order.currency, paid_on=str(order.paid_on), customer=order.customer_name,
               country=order.customer_country, payment_intent_id=pid)

        reading = read(req, self.use_llm)
        tr.add("READ", f"{reading.source}: intent={reading.intent.value} evidence={reading.evidence} "
                       f"injection={reading.injection_suspected} cites={reading.citations}",
               kind="reading", source=reading.source, intent=reading.intent.value, evidence=reading.evidence,
               injection=reading.injection_suspected, citations=reading.citations,
               claimed_amount=reading.claimed_amount, summary=reading.summary)

        # a chargeback that is already open decides the case before any policy runs
        if (out := self._switch_to_dispute(order, None, tr, "chargeback check")):
            return out
        tr.add("CHECK", "no chargeback on this payment", kind="dispute_check", open=0)

        refundable = p.refundable()
        d = decide(order, req, reading, self.policy, today, refundable)
        _decision(tr, "PLAN", d, refundable=refundable, claimed_amount=reading.claimed_amount)

        if d.action in (Action.DECLINE, Action.ESCALATE):
            tr.outcome = d.action.value
            return tr
        if d.action == Action.REQUEST_BANK_DETAILS:
            return self._bank_payout(order, req, d, tr)
        if d.needs_approval and not self._approve(d, tr):
            tr.outcome = "AWAITING_APPROVAL"
            return tr
        return self._card_refund(order, req, d, tr)

    def _approve(self, d: Decision, tr: Trace) -> bool:
        h = binding_hash(d.binding)
        tr.add("APPROVAL", f"asking a person to approve {d.binding} (binding {h})",
               kind="request", binding=d.binding, hash=h, reasons=d.reasons)
        ok = self.approver(d, h)
        tr.add("APPROVAL", "approved" if ok else "not approved; nothing moved", kind="answer", approved=ok, hash=h)
        return ok

    def _poll(self, fn, oid: str, done: set[str], tries: int = 8) -> dict:
        r = fn(oid)
        while r.get("status") not in done and tries > 0 and self.poll_s > 0:
            time.sleep(self.poll_s)
            r = fn(oid)
            tries -= 1
        return r

    def _card_refund(self, order: Order, req: RefundRequest, d: Decision, tr: Trace) -> Trace:
        pid = order.payment_intent_id
        hold = self.rid("refund", pid, req.case_id)
        try:
            self.ledger.hold(pid, hold, d.amount)
        except LedgerError as e:
            tr.add("GUARD", f"ledger refused the refund: {e}")
            tr.outcome = "BLOCKED_DUPLICATE"
            return tr
        if (hook := self.hooks.get("before_refund")):
            hook(order)
        if (out := self._switch_to_dispute(order, hold, tr, "pre-refund re-check")):
            return out
        tr.add("CHECK", "pre-refund re-check: no chargeback; sending refund", kind="dispute_check", open=0)
        try:
            r = self.awx.create_refund(hold, pid, d.amount, f"OnceBack {req.case_id}",
                                       {"case_id": req.case_id, "binding": binding_hash(d.binding)})
        except AirwallexError as e:
            if e.code == "duplicate_request" and (prev := self.awx.find_refund(pid, hold)):
                # our earlier attempt reached Airwallex (response was lost): adopt it, never resend
                tr.add("GUARD", f"request_id already used; found existing refund {prev['id']} -> adopting it")
                r = prev
            elif e.code in REFUND_BLOCKED_CODES and (out := self._switch_to_dispute(order, hold, tr, f"refund API said {e.code}")):
                return out
            else:
                self.ledger.release(pid, hold)
                tr.add("RESULT", f"refund rejected: {e.code} {e.message}")
                tr.outcome = "ESCALATE"
                return tr
        r = self._poll(self.awx.get_refund, r["id"], {"ACCEPTED", "SETTLED", "FAILED"})
        if r["status"] in ("RECEIVED", "ACCEPTED", "SETTLED"):
            self.ledger.commit(pid, hold, r["id"])
            tr.add("ACT", f"card refund {r['id']} {float(r['amount']):,.2f} {r['currency']} -> {r['status']}",
                   kind="refund", refund_id=r["id"], amount=float(r["amount"]), currency=r["currency"],
                   status=r["status"])
            tr.outcome = "REFUNDED_TO_CARD"
            return tr
        self.ledger.release(pid, hold)
        tr.add("NEW_INFO", f"card refund {r['id']} FAILED; the card rail is closed, switching to a bank payout")
        d2 = Decision(Action.REQUEST_BANK_DETAILS, d.amount, d.currency, d.reasons + ["card refund failed"],
                      True, dict(d.binding, rail=Action.PAYOUT_BANK.value))
        return self._bank_payout(order, req, d2, tr)

    # ------------------------------------------------------------------ chargebacks
    def _switch_to_dispute(self, order: Order, hold: str | None, tr: Trace, why: str) -> Trace | None:
        """If the payment now has a chargeback, stop the refund and answer the chargeback instead."""
        pid = order.payment_intent_id
        ds = [x for x in self.awx.list_disputes(pid) if x.get("status") in OPEN_DISPUTE]
        if not ds:
            return None
        if hold:
            self.ledger.release(pid, hold)
        dis = ds[0]
        amt = float(dis["amount"])
        reason = dis.get("reason") or {}
        tr.add("NEW_INFO", f"{why}: chargeback {dis['id']} {amt:,.2f} {dis['currency']} "
                           f"{reason.get('type', '')} ({reason.get('original_code', '')}) is open on {pid}",
               kind="dispute", dispute_id=dis["id"], amount=amt, currency=dis["currency"],
               reason=reason.get("type", ""), code=reason.get("original_code", ""), stage_name=dis.get("stage"))
        p = self.ledger.get(pid)
        d2 = decide_on_dispute(self.ledger.state(pid), amt, p.refunded, dis["currency"])
        _decision(tr, "REVISED", d2)
        if d2.action == Action.ACCEPT_DISPUTE:
            self._accept_dispute(order, dis, tr, "refund cancelled; accepting the chargeback instead of refunding")
        tr.outcome = d2.action.value
        return tr

    def _accept_dispute(self, order: Order, dis: dict, tr: Trace, why: str) -> None:
        status = dis.get("status")
        if status in NEEDS_RESPONSE:
            r = self.awx.accept_dispute(dis["id"], self.rid("accept", dis["id"]), why)
            status = r.get("status")
        self.ledger.chargeback(order.payment_intent_id, float(dis["amount"]))
        tr.add("ACT", f"accepted chargeback {dis['id']} -> {status}; no refund sent (no double pay)",
               kind="accept", dispute_id=dis["id"], status=status)

    def on_dispute(self, order: Order, case_id: str, tr: Trace | None = None) -> Trace:
        tr = tr or Trace(case_id)
        self.ledger.open(order.payment_intent_id, order.currency, order.amount)
        disputes = [d for d in self.awx.list_disputes(order.payment_intent_id)
                    if d.get("status") in NEEDS_RESPONSE]
        if not disputes:
            tr.add("CHECK", f"no chargeback needing a response on {order.payment_intent_id}")
            tr.outcome = "NO_OPEN_DISPUTE"
            return tr
        dis = disputes[0]
        amt = float(dis["amount"])
        tr.add("NEW_INFO", f"chargeback {dis['id']} {amt:,.2f} {dis['currency']} "
                           f"{(dis.get('reason') or {}).get('type', '')} ({(dis.get('reason') or {}).get('original_code', '')}) "
                           f"stage {dis.get('stage')} on {order.payment_intent_id}",
               kind="dispute", dispute_id=dis["id"], amount=amt, currency=dis["currency"],
               reason=(dis.get("reason") or {}).get("type", ""), code=(dis.get("reason") or {}).get("original_code", ""),
               stage_name=dis.get("stage"))
        p = self.ledger.get(order.payment_intent_id)
        d = decide_on_dispute(self.ledger.state(order.payment_intent_id), amt, p.refunded, dis["currency"])
        _decision(tr, "REVISED", d, refunded=p.refunded, dispute_amount=amt)
        if d.action == Action.ACCEPT_DISPUTE:
            self._accept_dispute(order, dis, tr, "no refund issued; accepting")
        elif d.action == Action.CHALLENGE_DISPUTE:
            from .evidence import refund_receipt_jpg
            lines = [f"order {order.order_id}  payment {order.payment_intent_id}",
                     f"refunded {p.refunded:,.2f} {dis['currency']} via {', '.join(p.refs)}",
                     f"customer {order.customer_name}  policy: changed-mind returns, 10% restocking fee"]
            fid = self.awx.upload_file(f"refund_{order.order_id}.jpg", refund_receipt_jpg(lines),
                                       "image/jpeg", "refund receipt")
            r = self.awx.challenge_dispute(dis["id"], self.rid("challenge", dis["id"]), [fid],
                                           f"Customer already refunded {p.refunded:.2f} {dis['currency']} "
                                           f"({', '.join(p.refs)}) per return policy",
                                           product_description=f"order {order.order_id}")
            tr.add("ACT", f"uploaded receipt {fid[:16]}…; challenged {dis['id']} reason CUSTOMER_REFUNDED "
                          f"-> {r.get('status')}",
                   kind="challenge", dispute_id=dis["id"], file_id=fid, status=r.get("status"), evidence=True,
                   refunded=p.refunded, refs=list(p.refs))
        tr.outcome = d.action.value
        return tr

    # ------------------------------------------------------------------ bank payouts
    def _bank_payout(self, order: Order, req: RefundRequest, d: Decision, tr: Trace) -> Trace:
        pid = order.payment_intent_id
        bd = self.bank_details(req, order)
        if bd is None:
            tr.add("RESULT", "card rail closed and no bank details yet; asked the customer for them")
            tr.outcome = "AWAITING_BANK_DETAILS"
            return tr
        acct = _account_fingerprint(bd)
        d.binding = dict(d.binding, beneficiary_account=acct,
                         beneficiary=f"{bd.account_name} · {_mask(bd)}")
        # the name check is against the BUYER on the order, not whatever name the customer typed
        try:
            v = self.awx.verify_account(bd.for_verify(order.customer_name))
        except AirwallexError as e:
            if e.code not in VERIFY_UNSUPPORTED_CODES:
                tr.add("RESULT", f"account verification failed: {e.code} {e.message}")
                tr.outcome = "AWAITING_BANK_DETAILS"
                return tr
            v = {"code": "CANNOT_VERIFY", "details": {}, "message": e.code}
        det = v.get("details") or {}
        match = det.get("account_name_match_result")
        tr.add("NEW_INFO", f"verify_account({bd.bank_country_code}): {v.get('code')} / {match} "
                           f"(bank holder '{det.get('resolved_account_name', '-')}', buyer '{order.customer_name}')",
               kind="name_check", code=v.get("code"), match=match, bank_holder=det.get("resolved_account_name"),
               buyer=order.customer_name, bank=det.get("resolved_bank_name"), country=bd.bank_country_code,
               account=_mask(bd))
        d2 = decide_payout(v.get("code"), match, d)
        _decision(tr, "REVISED", d2)
        if d2.action == Action.REQUEST_BANK_DETAILS:
            tr.outcome = "AWAITING_BANK_DETAILS"
            return tr
        if d2.action != Action.PAYOUT_BANK:
            tr.outcome = d2.action.value
            return tr
        if d2.needs_approval and not self._approve(d2, tr):
            tr.outcome = "AWAITING_APPROVAL"
            return tr

        hold = self.rid("payout", pid, req.case_id)
        try:
            self.ledger.hold(pid, hold, d2.amount)
        except LedgerError as e:
            tr.add("GUARD", f"ledger refused the payout: {e}")
            tr.outcome = "BLOCKED_DUPLICATE"
            return tr
        if (out := self._switch_to_dispute(order, hold, tr, "pre-payout re-check")):
            return out
        try:
            self._ensure_currency(d2.currency, d2.amount, hold, tr)
            first, _, last = bd.account_name.partition(" ")
            ben = {"entity_type": "PERSONAL", "first_name": first, "last_name": last or first,
                   "bank_details": dict(bd.as_api(), account_currency=d2.currency),
                   "address": dict(order.address, country_code=bd.bank_country_code)}
            b = self.awx.create_beneficiary(self.rid("beneficiary", hold), ben, ["LOCAL"],
                                            nickname=f"OnceBack {order.order_id}")
            t = self.awx.create_transfer(self.rid("transfer", hold), b["id"], d2.amount, d2.currency,
                                         f"Refund {order.order_id}")
        except AirwallexError as e:
            self.ledger.release(pid, hold)
            tr.add("RESULT", f"payout rejected: {e.code} {e.message}")
            tr.outcome = "ESCALATE"
            return tr
        tr.add("ACT", f"beneficiary {b['id']}; payout {t['id']} {d2.amount:,.2f} {d2.currency} via LOCAL "
                      f"-> {t['status']}", kind="payout", beneficiary_id=b["id"], transfer_id=t["id"],
               amount=d2.amount, currency=d2.currency, status=t["status"])
        if self.simulate_settlement:
            for st in ("SENT", "PAID"):
                try:
                    self.awx.sim.transfer(t["id"], st)
                except AirwallexError as e:
                    tr.add("NEW_INFO", f"sandbox transition {st} refused: {e.code}")
        t = self._poll(self.awx.get_transfer, t["id"], {"PAID", "CANCELLED", "FAILED"})
        if t["status"] == "PAID":
            self.ledger.commit(pid, hold, t["id"])
            tr.add("RESULT", f"payout {t['id']} PAID", kind="paid", transfer_id=t["id"])
            tr.outcome = "REFUNDED_TO_BANK"
        elif t["status"] in ("CANCELLED", "FAILED"):
            self.ledger.release(pid, hold)
            tr.add("RESULT", f"payout {t['id']} {t['status']}; money back in the wallet, needs a person")
            tr.outcome = "ESCALATE"
        else:
            tr.add("RESULT", f"payout {t['id']} {t['status']}; held in the ledger until it settles")
            tr.outcome = "PAYOUT_IN_FLIGHT"
        return tr

    def _ensure_currency(self, cur: str, amount: float, hold: str, tr: Trace) -> None:
        have = self.awx.balances().get(cur, 0.0)
        if have >= amount:
            tr.add("CHECK", f"wallet holds {have:,.2f} {cur}; no conversion needed", kind="balance",
                   currency=cur, available=have)
            return
        q = self.awx.quote(self.home, cur, amount)
        c = self.awx.convert(self.rid("fx", hold), self.home, cur, amount, q.get("quote_id"))
        sell = float(c.get("sell_amount") or q.get("sell_amount") or 0)
        tr.add("ACT", f"FX: bought {amount:,.2f} {cur} for {sell:,.2f} {self.home} at {q.get('client_rate')} "
                      f"(quote {q.get('quote_id')}, conversion {c.get('conversion_id') or c.get('id')} {c.get('status')})",
               kind="fx", buy=cur, buy_amount=amount, sell=self.home, sell_amount=sell, rate=q.get("client_rate"),
               quote_id=q.get("quote_id"), status=c.get("status"))
