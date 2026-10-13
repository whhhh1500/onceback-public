"""In-memory stand-in for the Airwallex sandbox. Every behaviour below was
checked against the real sandbox on 2026-10-10 (fixtures in tests/fixtures/):

- ANY reuse of a request_id (same body or not) -> 400 duplicate_request; the
  original object is found again via GET /pa/refunds?payment_intent_id=
- refunds on a payment with ANY dispute (open or accepted) -> 400 amount_above_limit
  "The operating amount X is above the limit 0.00."
- refunds are RECEIVED on create and ACCEPTED when read back
- verify_account: GB -> VERIFIED + FULL_MATCH / PARTIAL_MATCH / NOT_MATCHED;
  JP -> 400 transfer_method_unsupported
- beneficiary create returns {id, beneficiary}; transfers start SCHEDULED,
  simulation SENT -> PAID (PROCESSING is rejected with 500 operation_failed)
- FX conversions settle immediately
- insufficient balance on transfers -> balance_insufficient
"""
from __future__ import annotations

import difflib
import itertools
import re
from typing import Any

from .airwallex import AirwallexError

RATES_USD_PER_UNIT = {"USD": 1.0, "EUR": 1.087, "GBP": 1.266, "JPY": 0.00667}
REASONS = {"13.1": ("PRODUCT_NOT_RECEIVED", "Merchandise/ Services Not Received"),
           "13.6": ("CREDIT_NOT_PROCESSED", "Credit Not Processed"),
           "10.4": ("FRAUD", "Other Fraud - Card Absent Environment")}
UNSUPPORTED_VERIFY = {"JP"}


def _norm(name: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z ]", " ", name.lower()).split() if t]


def name_match(given: str, registered: str) -> str:
    """Confirmation-of-Payee style answer, tuned to what the sandbox returned for
    holder "Betty Morris": exact -> FULL, one-letter typo -> PARTIAL, initials or
    an extra title -> NOT_MATCHED."""
    g, r = _norm(given), _norm(registered)
    if sorted(g) == sorted(r):
        return "FULL_MATCH"
    if len(g) == len(r) and difflib.SequenceMatcher(None, " ".join(g), " ".join(r)).ratio() >= 0.9:
        return "PARTIAL_MATCH"
    return "NOT_MATCHED"


class MockTransport:
    def __init__(self, balances: dict[str, float] | None = None,
                 bank_registry: dict[str, str] | None = None, settlement_currency: str | None = "USD"):
        self.balances = dict(balances or {"USD": 20000.0})
        # Merchant settles card payments into one currency (None = settle like-for-like).
        self.settle = settlement_currency
        self.bank_registry = dict(bank_registry or {})   # account key -> registered holder name
        self.intents: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {}
        self.beneficiaries: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self._req: dict[str, str] = {}
        self._seq = itertools.count(1)

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._seq):04d}"

    def _once(self, request_id: str, op: str) -> str | None:
        """Real sandbox: ANY reuse of a request_id (same body or not) -> 400 duplicate_request."""
        if request_id in self._req:
            raise AirwallexError(400, "duplicate_request",
                                 f"The request ID {request_id} has been used previously.")
        return None

    def _remember(self, request_id: str, op: str, oid: str) -> None:
        self._req[request_id] = f"{op}#{oid}"

    def _settle_amount(self, amount: float, currency: str) -> tuple[str, float]:
        if not self.settle or self.settle == currency:
            return currency, amount
        return self.settle, round(amount * RATES_USD_PER_UNIT[currency] / RATES_USD_PER_UNIT[self.settle], 2)

    @staticmethod
    def _account_key(bd: dict) -> str:
        return f"{bd.get('account_routing_value1') or bd.get('swift_code') or ''}:{bd.get('account_number') or bd.get('iban') or ''}"

    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> Any:
        self.calls.append((method, path))
        b = json or {}
        p = [x for x in path.split("/") if x]

        if path == "/pa/payment_intents/create":
            self._once(b["request_id"], "pi")
            oid = self._id("int")
            self.intents[oid] = {"id": oid, "amount": b["amount"], "currency": b["currency"],
                                 "merchant_order_id": b.get("merchant_order_id"), "status": "REQUIRES_PAYMENT_METHOD",
                                 "captured_amount": 0, "refunded": 0.0, "client_secret": "mock_client_secret"}
            self._remember(b["request_id"], "pi", oid)
            return self.intents[oid]

        if p[:2] == ["pa", "payment_intents"] and len(p) == 4 and p[3] == "confirm":
            pi = self.intents[p[2]]
            self._once(b["request_id"], "confirm")
            pi.update(status="SUCCEEDED", captured_amount=pi["amount"])
            self._remember(b["request_id"], "confirm", pi["id"])
            cur, amt = self._settle_amount(pi["amount"], pi["currency"])
            self.balances[cur] = self.balances.get(cur, 0) + amt
            return pi

        if p[:2] == ["pa", "payment_intents"] and len(p) == 3:
            return self.intents[p[2]]

        if path == "/pa/refunds/create":
            self._once(b["request_id"], "rfd")
            pi = self.intents[b["payment_intent_id"]]
            amt = float(b["amount"])
            if any(d["payment_intent_id"] == pi["id"] for d in self.disputes.values()):
                raise AirwallexError(400, "amount_above_limit",
                                     f"The operating amount {amt:g} is above the limit 0.00.")
            if pi["status"] != "SUCCEEDED":
                raise AirwallexError(400, "invalid_status_for_operation", pi["status"])
            if amt > pi["captured_amount"] - pi["refunded"] + 1e-9:
                raise AirwallexError(400, "amount_above_limit",
                                     f"The operating amount {amt:g} is above the limit "
                                     f"{pi['captured_amount'] - pi['refunded']:.2f}.")
            scur, samt = self._settle_amount(amt, pi["currency"])
            if self.balances.get(scur, 0) < samt:
                return self._store_refund(b, pi, amt, "FAILED")
            pi["refunded"] += amt
            self.balances[scur] -= samt
            return self._store_refund(b, pi, amt, "RECEIVED")

        if path == "/pa/refunds":
            return {"items": [r for r in self.refunds.values()
                              if r["payment_intent_id"] == (params or {}).get("payment_intent_id")]}

        if p[:2] == ["pa", "refunds"] and len(p) == 3:
            r = self.refunds[p[2]]
            if r["status"] == "RECEIVED":
                r["status"] = "ACCEPTED"
            return r

        if path == "/simulation/pa/payment_disputes/create":
            pi = self.intents[b["payment_intent_id"]]
            typ, desc = REASONS.get(b.get("reason_code", "13.1"), ("GENERAL", "General"))
            oid = self._id("dis")
            self.disputes[oid] = {
                "id": oid, "stage": b.get("stage", "CHARGEBACK"), "status": "REQUIRES_RESPONSE",
                "amount": float(b["amount"]), "currency": pi["currency"], "payment_intent_id": pi["id"],
                "merchant_order_id": pi.get("merchant_order_id"), "issuer_comment": b.get("comment", ""),
                "reason": {"original_code": b.get("reason_code", "13.1"), "description": desc, "type": typ},
                "refunds": [{"id": r["id"]} for r in self.refunds.values() if r["payment_intent_id"] == pi["id"]],
                "accept_details": [], "challenge_details": [], "due_at": b.get("due_at")}
            return self.disputes[oid]

        if path == "/pa/payment_disputes":
            pid = (params or {}).get("payment_intent_id")
            return {"items": [d for d in self.disputes.values() if d["payment_intent_id"] == pid]}

        if p[:2] == ["pa", "payment_disputes"] and len(p) == 3:
            return self.disputes[p[2]]

        if p[:2] == ["pa", "payment_disputes"] and len(p) == 4 and p[3] == "accept":
            d = self.disputes[p[2]]
            self._once(b["request_id"], "accept")
            if d["status"] != "REQUIRES_RESPONSE":
                raise AirwallexError(400, "invalid_status_for_operation", d["status"])
            d["status"] = "ACCEPTED"
            d["accept_details"].append({"stage": d["stage"], "reason": b.get("reason"),
                                        "description": b.get("description"), "accepted_by": b.get("accepted_by")})
            scur, samt = self._settle_amount(d["amount"], d["currency"])
            self.balances[scur] = self.balances.get(scur, 0) - samt
            self._remember(b["request_id"], "accept", d["id"])
            return d

        if p[:2] == ["pa", "payment_disputes"] and len(p) == 4 and p[3] == "challenge":
            d = self.disputes[p[2]]
            self._once(b["request_id"], "challenge")
            if d["status"] != "REQUIRES_RESPONSE":
                raise AirwallexError(400, "invalid_status_for_operation", d["status"])
            docs = (b.get("supporting_documents") or {}).get("documents") or []
            if not docs or any(f not in self.files for doc in docs for f in doc.get("file_ids", [])):
                raise AirwallexError(400, "validation_error", "supporting_documents.file_ids")
            d["status"] = "CHALLENGED"
            d["challenge_details"].append({"stage": d["stage"], "reason": b.get("reason"),
                                           "challenged_by": b.get("challenged_by")})
            self._remember(b["request_id"], "challenge", d["id"])
            return d

        if path == "FILES/files/upload":
            oid = f"{self._id('file')}_{b['filename']}"
            self.files[oid] = {"file_id": oid, "filename": b["filename"], "size": len(b.get("file") or b""),
                               "notes": b.get("notes", "")}
            return self.files[oid]

        if path == "/beneficiaries/verify_account":
            bd = b["bank_details"]
            if bd.get("bank_country_code") in UNSUPPORTED_VERIFY:
                raise AirwallexError(400, "transfer_method_unsupported", "Unsupported transfer method.")
            holder = self.bank_registry.get(self._account_key(bd))
            if holder is None:
                return {"code": "NOT_VERIFIED", "details": {}, "message": "Account could not be verified."}
            match = name_match(bd.get("account_name", ""), holder)
            det = {"account_name_match_result": match, "resolved_bank_name": "Mock Bank"}
            if match != "NOT_MATCHED":
                det["resolved_account_name"] = holder
            return {"code": "VERIFIED", "details": det,
                    "message": "This is a valid account. Refer to the details object for verification details."}

        if path == "/beneficiaries/create":
            self._once(b["request_id"], "ben")
            ben = b["beneficiary"]
            bd = ben.get("bank_details", {})
            if bd.get("bank_country_code") == "JP" and "LOCAL" in b.get("transfer_methods", []) \
                    and not bd.get("bank_account_category"):
                raise AirwallexError(400, "validation_failed", "beneficiary.bank_details.bank_account_category")
            oid = self._id("ben")
            self.beneficiaries[oid] = {"id": oid, "beneficiary": ben, "nickname": b.get("nickname"),
                                       "transfer_methods": b.get("transfer_methods")}
            self._remember(b["request_id"], "ben", oid)
            return self.beneficiaries[oid]

        if path == "/balances/current":
            return [{"currency": c, "available_amount": round(v, 2), "total_amount": round(v, 2)}
                    for c, v in self.balances.items()]

        if path == "/fx/quotes/create":
            qid = self._id("qt")
            rate = RATES_USD_PER_UNIT[b["buy_currency"]] / RATES_USD_PER_UNIT[b["sell_currency"]] * 1.004
            # real client_rate is quoted as buy units per 1 sell unit (USDJPY 157.22)
            self.quotes[qid] = {"quote_id": qid, "client_rate": round(1 / rate, 5), "used": False, **b,
                                "sell_amount": round(b["buy_amount"] * rate, 2), "usage": "MULTI_USE"}
            return self.quotes[qid]

        if path == "/fx/conversions/create":
            self._once(b["request_id"], "conv")
            q = self.quotes.get(b.get("quote_id") or "")
            rate = RATES_USD_PER_UNIT[b["buy_currency"]] / RATES_USD_PER_UNIT[b["sell_currency"]] * 1.004
            sell = q["sell_amount"] if q else round(b["buy_amount"] * rate, 2)
            if q:
                q["used"] = True
            if self.balances.get(b["sell_currency"], 0) < sell:
                raise AirwallexError(400, "insufficient_fund", b["sell_currency"])
            self.balances[b["sell_currency"]] -= sell
            self.balances[b["buy_currency"]] = self.balances.get(b["buy_currency"], 0) + b["buy_amount"]
            oid = self._id("conv")
            self._remember(b["request_id"], "conv", oid)
            return {"conversion_id": oid, "status": "SETTLED", "sell_amount": sell, "buy_amount": b["buy_amount"],
                    "client_rate": q["client_rate"] if q else round(1 / rate, 5)}

        if path == "/transfers/create":
            self._once(b["request_id"], "tfr")
            cur, amt = b["transfer_currency"], b["transfer_amount"]
            if b["beneficiary_id"] not in self.beneficiaries:
                raise AirwallexError(400, "resource_not_found", "beneficiary")
            if self.balances.get(cur, 0) < amt:
                raise AirwallexError(400, "balance_insufficient", cur)
            self.balances[cur] -= amt
            oid = self._id("tfr")
            self.transfers[oid] = {"id": oid, "status": "SCHEDULED", **b}
            self._remember(b["request_id"], "tfr", oid)
            return self.transfers[oid]

        if p[:2] == ["simulation", "transfers"]:
            t = self.transfers[p[2]]
            nxt = b["next_status"]
            if nxt == "PROCESSING":
                raise AirwallexError(500, "operation_failed", "Request could not be processed")
            if nxt == "PAID" and t["status"] != "SENT":
                raise AirwallexError(400, "invalid_transition", "simulate SENT before PAID")
            if nxt == "FAILED":
                t.update(status="CANCELLED", failure_type=b.get("failure_type", "BANK_RETURN"))
                self.balances[t["transfer_currency"]] += t["transfer_amount"]
            else:
                t["status"] = nxt
            return t

        if p[0] == "transfers" and len(p) == 2:
            return self.transfers[p[1]]

        if path == "/simulation/deposit/create":
            return {"id": self._id("dep"), "amount": b["amount"], "status": "PENDING"}

        raise AirwallexError(404, "not_found", f"{method} {path}")

    def _store_refund(self, b, pi, amt, status):
        oid = self._id("rfd")
        self.refunds[oid] = {"id": oid, "request_id": b["request_id"], "payment_intent_id": pi["id"], "amount": amt,
                             "currency": pi["currency"], "status": status, "reason": b.get("reason")}
        self._remember(b["request_id"], "rfd", oid)
        return self.refunds[oid]
