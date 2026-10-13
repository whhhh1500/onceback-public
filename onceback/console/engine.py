"""Runs OnceBack cases for the console.

Each case runs in its own worker thread. When the agent needs a person, the
approver callback blocks that thread on a Pending approval until someone
answers in the UI with the exact binding hash it was shown (or it times out:
nothing moves). Chargebacks arrive either as signed Airwallex webhooks or via
the dispute poller; both feed one set of handled dispute ids, so a dispute is
never worked twice.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from ..agent import OnceBack, Trace
from ..airwallex import Airwallex, AirwallexError
from ..demo import BANK, ORDERS, REQUESTS, TEST_CARD, TODAY
from ..ledger import request_id
from ..models import Decision, Order, RefundRequest

RACE_ORDER = "ORD-1003"   # C3: the shopper files a chargeback while the refund is being prepared


@dataclass
class Pending:
    case_id: str
    decision: Decision
    hash: str
    created: float = field(default_factory=time.time)
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None          # "approve" | "reject" | "timeout"
    approver: str = ""
    answered_at: float | None = None

    def public(self) -> dict:
        d = self.decision
        return {"case_id": self.case_id, "hash": self.hash, "action": d.action.value, "amount": d.amount,
                "currency": d.currency, "binding": d.binding, "reasons": d.reasons, "created_at": self.created,
                "answer": self.answer, "approver": self.approver, "answered_at": self.answered_at}


@dataclass
class Case:
    id: str
    kind: str                              # "request" | "chargeback"
    order_id: str
    request: RefundRequest | None = None
    source: str = ""                       # chargebacks: "webhook" | "poll" | "manual"
    dispute_id: str = ""
    status: str = "new"                    # new | running | awaiting_approval | done | error
    outcome: str = ""
    error: str = ""
    trace: Trace | None = None
    opened_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def summary(self, orders: dict[str, Order]) -> dict:
        o = orders.get(self.order_id)
        od = next((x for x in ORDERS if x["order_id"] == self.order_id), {})
        return {"id": self.id, "kind": self.kind, "order_id": self.order_id,
                "message": self.request.message if self.request else "",
                "attachments": self.request.attachments if self.request else [],
                "received_on": str(self.request.received_on) if self.request else "",
                "customer": od.get("customer_name", ""), "country": od.get("customer_country", ""),
                "amount": od.get("amount"), "currency": od.get("currency", ""),
                "paid_on": str(od.get("paid_on", "")), "payment_intent_id": o.payment_intent_id if o else "",
                "status": self.status, "outcome": self.outcome, "source": self.source,
                "dispute_id": self.dispute_id, "error": self.error, "opened_at": self.opened_at}

    def detail(self, orders: dict[str, Order]) -> dict:
        d = self.summary(orders)
        d["steps"] = list(self.trace.steps) if self.trace else []
        return d


class Engine:
    def __init__(self, awx: Airwallex, live: bool = False, use_llm: bool = False,
                 webhook_secret: str | None = None, approval_timeout_s: float = 900.0, today: date = TODAY):
        self.awx, self.live, self.today = awx, live, today
        self.webhook_secret = webhook_secret
        self.approval_timeout_s = approval_timeout_s
        self.tag = f"console-{uuid.uuid4().hex[:8]}" if live else ""
        self.lock = threading.RLock()
        self.agent = OnceBack(awx, approver=self._approver, bank_details=lambda r, o: BANK.get(r.case_id),
                              use_llm=use_llm, poll_s=1.5 if live else 0.0, run_tag=self.tag)
        self.agent.hooks["before_refund"] = self._race
        self.orders: dict[str, Order] = {}
        self.cases: dict[str, Case] = {r.case_id: Case(r.case_id, "request", r.order_id, r) for r in REQUESTS}
        self.pending: dict[str, Pending] = {}
        self.activity: list[dict] = []
        self.handled_disputes: set[str] = set()
        self.seen_events: set[str] = set()
        self.setup_state = {"status": "idle", "done": 0, "total": len(ORDERS), "error": ""}
        self.balances: dict[str, float] = {}
        self._local = threading.local()
        self._stop = threading.Event()
        self._raced: set[str] = set()

    # ---------- activity feed
    def log(self, kind: str, text: str, **data) -> None:
        with self.lock:
            self.activity.append({"at": time.time(), "kind": kind, "text": text, **data})
            del self.activity[:-300]

    def _refresh_balances(self) -> None:
        try:
            b = self.awx.balances()
            self.balances = {k: v for k, v in b.items() if v}
        except Exception as e:  # display only
            self.log("error", f"balances: {e}")

    # ---------- demo orders
    def setup(self) -> None:
        with self.lock:
            if self.setup_state["status"] in ("running", "ready"):
                return
            self.setup_state.update(status="running", done=0, error="")
        threading.Thread(target=self._setup, daemon=True, name="setup").start()

    def _setup(self) -> None:
        shopper = None
        try:
            if self.live:
                from ..shopper import Shopper
                shopper = Shopper()
            for od in ORDERS:
                oid = od["order_id"]
                pi = self.awx.create_payment_intent(request_id(self.tag, "pi", oid), od["amount"], od["currency"],
                                                    f"{oid}-{self.tag}" if self.tag else oid)
                if shopper:
                    r = shopper.pay(pi["id"], pi["client_secret"], od["currency"])
                    if r.get("error"):
                        raise RuntimeError(f"test shopper could not pay {oid}: {r['error']}")
                    for _ in range(15):
                        st = self.awx.get_payment_intent(pi["id"])["status"]
                        if st == "SUCCEEDED":
                            break
                        time.sleep(1)
                else:
                    st = self.awx.confirm_payment_intent(pi["id"], request_id(self.tag, "confirm", oid),
                                                         TEST_CARD)["status"]
                with self.lock:
                    self.orders[oid] = Order(payment_intent_id=pi["id"], **od)
                    self.agent.ledger.open(pi["id"], od["currency"], od["amount"])
                    self.setup_state["done"] += 1
                self.log("payment", f"{oid} paid {od['amount']:,.2f} {od['currency']} with test card ****0008 "
                                    f"-> {pi['id']} {st}", order_id=oid, payment_intent_id=pi["id"])
            self._refresh_balances()
            with self.lock:
                self.setup_state["status"] = "ready"
        except Exception as e:
            with self.lock:
                self.setup_state.update(status="error", error=f"{type(e).__name__}: {e}")
            self.log("error", f"setup failed: {e}")
        finally:
            if shopper:
                shopper.close()

    # ---------- running cases
    def run_case(self, case_id: str) -> None:
        with self.lock:
            if self.setup_state["status"] != "ready":
                raise RuntimeError("prepare the demo orders first")
            c = self.cases[case_id]
            if c.status in ("running", "awaiting_approval"):
                raise RuntimeError(f"{case_id} is already running")
            if c.status == "done" and c.kind == "chargeback":
                raise RuntimeError(f"{case_id} is already handled")
            c.status, c.outcome, c.error, c.finished_at = "running", "", "", None
            c.trace = Trace(case_id, listener=self._on_step)
        threading.Thread(target=self._work, args=(c,), daemon=True, name=f"case-{case_id}").start()

    def _on_step(self, tr: Trace, step: dict) -> None:
        if step["stage"] in ("ACT", "RESULT", "NEW_INFO", "GUARD"):
            self.log("agent", f"{tr.case_id} · {step['text']}", case_id=tr.case_id)

    def _work(self, c: Case) -> None:
        self._local.case_id = c.id
        o = self.orders[c.order_id]
        try:
            if c.kind == "request":
                self.agent.handle(o, c.request, self.today, c.trace)
            else:
                self.agent.on_dispute(o, c.id, c.trace)
            outcome = c.trace.outcome
            p = self.pending.get(c.id)
            if outcome == "AWAITING_APPROVAL" and p and p.answer == "reject":
                outcome = "REJECTED_BY_APPROVER"
            with self.lock:
                c.outcome, c.status = outcome, "done"
            self.log("agent", f"{c.id} -> {outcome}", case_id=c.id, outcome=outcome)
        except Exception as e:  # keep the console alive; show the error on the case
            with self.lock:
                c.status, c.error = "error", f"{type(e).__name__}: {e}"
            self.log("error", f"{c.id}: {e}", case_id=c.id)
        finally:
            with self.lock:
                c.finished_at = time.time()
            self._refresh_balances()

    def _race(self, order: Order) -> None:
        """Sandbox simulation for C3: the chargeback appears while the refund is being prepared."""
        if order.order_id != RACE_ORDER or order.order_id in self._raced:
            return
        self._raced.add(order.order_id)
        d = self.awx.sim.dispute(order.payment_intent_id, order.amount, "13.1", comment="item not received")
        with self.lock:
            self.handled_disputes.add(d["id"])     # the running case answers it, not the poller
        self.log("sandbox", f"shopper filed chargeback {d['id']} on {order.order_id} while the refund was being prepared",
                 order_id=order.order_id, dispute_id=d["id"])

    # ---------- approvals
    def _approver(self, d: Decision, h: str) -> bool:
        cid = getattr(self._local, "case_id", None) or "?"
        p = Pending(cid, d, h)
        with self.lock:
            self.pending[cid] = p
            if cid in self.cases:
                self.cases[cid].status = "awaiting_approval"
        self.log("approval", f"{cid} waiting for approval: {d.action.value} {d.amount:,.2f} {d.currency} "
                             f"(binding {h})", case_id=cid)
        if not p.event.wait(self.approval_timeout_s):
            with self.lock:
                p.answer, p.answered_at = "timeout", time.time()
            self.log("approval", f"{cid} approval timed out; nothing moved", case_id=cid)
        with self.lock:
            if cid in self.cases:
                self.cases[cid].status = "running"
        return p.answer == "approve"

    def answer(self, case_id: str, h: str, decision: str, approver: str = "operator") -> Pending:
        with self.lock:
            p = self.pending.get(case_id)
            if p is None:
                raise KeyError(case_id)
            if p.answer is not None:
                raise ValueError("this approval was already answered")
            if h != p.hash:
                raise ValueError("hash does not match the pending terms")
            p.answer, p.approver, p.answered_at = decision, approver, time.time()
        self.log("approval", f"{case_id} {decision}d by {approver} (binding {h})", case_id=case_id)
        p.event.set()
        return p

    # ---------- chargebacks: simulation, poller, webhooks
    def simulate_chargeback(self, order_id: str) -> dict:
        o = self.orders[order_id]
        d = self.awx.sim.dispute(o.payment_intent_id, o.amount, "13.6", comment="credit not processed")
        self.log("sandbox", f"sandbox: chargeback {d['id']} {o.amount:,.2f} {o.currency} filed on {order_id}",
                 order_id=order_id, dispute_id=d["id"])
        return d

    def dispute_requires_response(self, payment_intent_id: str, dispute_id: str, source: str) -> Case | None:
        with self.lock:
            if dispute_id in self.handled_disputes:
                return None
            o = next((x for x in self.orders.values() if x.payment_intent_id == payment_intent_id), None)
            if o is None:
                return None
            self.handled_disputes.add(dispute_id)
            cid = f"{o.order_id}-chargeback"
            c = Case(cid, "chargeback", o.order_id, source=source, dispute_id=dispute_id)
            self.cases[cid] = c
        self.log(source, f"chargeback {dispute_id} on {o.order_id} needs a response -> case {cid}",
                 case_id=cid, dispute_id=dispute_id)
        self.run_case(cid)
        return c

    def poll_once(self) -> dict:
        opened = 0
        if self.setup_state["status"] != "ready":
            return {"opened": 0}
        for o in list(self.orders.values()):
            try:
                ds = self.awx.list_disputes(o.payment_intent_id)
            except AirwallexError as e:
                self.log("error", f"poll {o.order_id}: {e.code}")
                continue
            for d in ds:
                if d.get("status") == "REQUIRES_RESPONSE" and self.dispute_requires_response(
                        o.payment_intent_id, d["id"], "poll"):
                    opened += 1
        return {"opened": opened}

    def start_poller(self, interval_s: float) -> None:
        def loop():
            while not self._stop.wait(interval_s):
                try:
                    self.poll_once()
                except Exception as e:
                    self.log("error", f"poller: {e}")
        threading.Thread(target=loop, daemon=True, name="poller").start()

    def stop(self) -> None:
        self._stop.set()
        for p in list(self.pending.values()):
            p.event.set()

    def handle_event(self, event: dict) -> dict:
        eid, name = event.get("id", ""), event.get("name", "")
        obj = (event.get("data") or {}).get("object") or {}
        with self.lock:
            if eid in self.seen_events:
                return {"status": "duplicate"}
            self.seen_events.add(eid)
        self.log("webhook", f"{name} {obj.get('id', '')} {obj.get('status', '')}".strip(), event_id=eid)
        if name == "payment_dispute.requires_response" and obj.get("payment_intent_id"):
            c = self.dispute_requires_response(obj["payment_intent_id"], obj.get("id", eid), "webhook")
            return {"status": "case_opened" if c else "noted"}
        return {"status": "noted"}

    # ---------- views
    def ledger_view(self) -> list[dict]:
        out = []
        for o in self.orders.values():
            p = self.agent.ledger.get(o.payment_intent_id)
            out.append({"order_id": o.order_id, "currency": o.currency, "captured": p.captured,
                        "refunded": p.refunded, "chargeback": p.disputed, "in_flight": sum(p.in_flight.values()),
                        "returned": self.agent.ledger.total_returned(o.payment_intent_id), "refs": p.refs,
                        "payment_intent_id": o.payment_intent_id, "customer": o.customer_name})
        return out

    def state(self) -> dict:
        with self.lock:
            return {"mode": "live" if self.live else "mock", "tag": self.tag, "setup": dict(self.setup_state),
                    "today": str(self.today), "webhook_configured": bool(self.webhook_secret),
                    "cases": [c.summary(self.orders) for c in self.cases.values()],
                    "approvals": [p.public() for p in self.pending.values() if p.answer is None],
                    "ledger": self.ledger_view(), "activity": list(reversed(self.activity[-60:])),
                    "balances": self.balances}

    def case_detail(self, case_id: str) -> dict:
        with self.lock:
            c = self.cases[case_id]
            d = c.detail(self.orders)
            p = self.pending.get(case_id)
            d["approval"] = p.public() if p else None
            return d
