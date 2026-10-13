"""Thin typed Airwallex client, matched to real sandbox responses (2026-10-10).

Base: https://api.sandbox.airwallex.com/api/v1 (files: https://files.sandbox.airwallex.com/api/v1)
Verified live (see ../LIVE_RUN.md and tests/fixtures/):
  POST /authentication/login               -> {token, expires_at}; repeated logins can return
                                              400 "API access has been disabled" -> retry/back off
  POST /pa/payment_intents/create          -> status REQUIRES_PAYMENT_METHOD, client_secret
  confirm                                  -> server-side raw-card confirm is NOT enabled for this
                                              account ("not enabled for native API access"); we pay
                                              via Airwallex.js Card element (onceback.shopper)
  POST /pa/refunds/create                  -> RECEIVED, then GET -> ACCEPTED; replaying a request_id
                                              -> 400 duplicate_request (NOT the original object);
                                              GET /pa/refunds?payment_intent_id= lists {id, request_id}
                                              while a dispute is open/accepted: 400 amount_above_limit
                                              ("above the limit 0.00")
  POST /simulation/pa/payment_disputes/create {payment_intent_id, amount, reason_code, stage, due_at}
  GET  /pa/payment_disputes?payment_intent_id=  -> {items:[{id, stage, status, reason{type}, refunds}]}
  POST /pa/payment_disputes/{id}/accept    {request_id, reason, accepted_by, description}
  POST /pa/payment_disputes/{id}/challenge {request_id, reason: CUSTOMER_REFUNDED, product_type,
                                            supporting_documents{documents[{type, file_ids}]}}
  POST files/upload (multipart)            -> {file_id}
  POST /beneficiaries/verify_account       -> GB: VERIFIED + FULL_MATCH|PARTIAL_MATCH|NOT_MATCHED;
                                              JP: 400 transfer_method_unsupported
  POST /beneficiaries/create               -> {id, beneficiary{...}}; JP LOCAL needs
                                              bank_account_category (Savings|Checking), else 400
                                              validation_failed
  POST /fx/quotes/create                   -> {quote_id, client_rate (buy units per 1 sell unit),
                                              sell_amount, usage MULTI_USE, valid_to_at}
  POST /fx/conversions/create              -> {conversion_id, status SETTLED, sell_amount}
  POST /transfers/create                   -> status SCHEDULED, funding FUNDED
  POST /simulation/transfers/{id}/transition -> SENT, then PAID (PROCESSING answers 500)
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any, Protocol

SECRET_KEYS = {"client_secret", "token", "x-api-key", "api_key", "authorization",
               "business_registration_number"}   # payer profile echoed in transfer responses


class AirwallexError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class Transport(Protocol):
    def request(self, method: str, path: str, json: dict | None = None,
                params: dict | None = None) -> Any: ...


def redact(o: Any) -> Any:
    """Strip anything secret before a request/response is written to disk."""
    if isinstance(o, dict):
        return {k: ("<redacted>" if k.lower() in SECRET_KEYS and v else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    return o


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Loads onceback/.env (KEY=VALUE lines) into os.environ without overriding
    variables that are already set; returns the merged view."""
    p = pathlib.Path(path) if path else pathlib.Path(__file__).resolve().parents[1] / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return {k: v for k, v in os.environ.items() if k.startswith(("AWX_", "CC1500_", "ONCEBACK_"))}


class LiveTransport:
    """Real HTTP to the Airwallex sandbox. Paths starting with FILES go to the file service."""

    def __init__(self, base_url: str | None = None, client_id: str | None = None, api_key: str | None = None,
                 record_to: str | None = None, timeout: float = 30.0):
        import requests  # only needed live
        env = load_env()
        base_url = base_url or env.get("AWX_BASE_URL") or "https://api.sandbox.airwallex.com/api/v1"
        client_id, api_key = client_id or env.get("AWX_CLIENT_ID"), api_key or env.get("AWX_API_KEY")
        if not client_id or not api_key:
            raise SystemExit("AWX_CLIENT_ID / AWX_API_KEY missing (put them in onceback/.env)")
        self._rq = requests
        self.base = base_url.rstrip("/")
        self.files_base = self.base.replace("://api.", "://files.")
        self.client_id, self.api_key = client_id, api_key
        self.record_to, self.timeout = record_to, timeout
        self._tok: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._tok or time.time() > self._tok["exp"]:
                last = None
                for i in range(8):
                    r = self._rq.post(self.base + "/authentication/login", timeout=self.timeout,
                                      headers={"x-client-id": self.client_id, "x-api-key": self.api_key})
                    try:
                        last = r.json()
                    except ValueError:
                        last = {"message": r.text}
                    if r.status_code < 300 and "token" in last:
                        break
                    time.sleep(2 * (i + 1))   # sandbox sometimes answers "API access has been disabled"
                else:
                    raise AirwallexError(r.status_code, "login_failed", str(last.get("message", ""))[:200])
                self._tok = {"t": last["token"], "exp": time.time() + 25 * 60}
            return self._tok["t"]

    def _record(self, method, path, body, params, status, resp) -> None:
        if not self.record_to:
            return
        e = {"method": method, "path": path, "json": body, "params": params, "status": status, "response": resp}
        with self._lock, open(self.record_to, "a") as f:
            f.write(json.dumps(redact(e)) + "\n")

    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> Any:
        for attempt in (0, 1):
            hdr = {"Authorization": "Bearer " + self._token(force=attempt == 1)}
            if path.startswith("FILES"):
                body = dict(json or {})
                data = body.pop("file")
                r = self._rq.post(self.files_base + path[len("FILES"):], headers=hdr, timeout=self.timeout,
                                  files={"file": (body["filename"], data, body.get("content_type", "application/octet-stream"))},
                                  data={"notes": body.get("notes", "")})
                rec_body = dict(body, size=len(data))
            else:
                r = self._rq.request(method, self.base + path, json=json, params=params, headers=hdr,
                                     timeout=self.timeout)
                rec_body = json
            if r.status_code == 401 and attempt == 0:
                continue
            break
        try:
            resp = r.json()
        except ValueError:
            resp = r.text
        self._record(method, path, rec_body, params, r.status_code, resp)
        if r.status_code >= 300:
            d = resp if isinstance(resp, dict) else {}
            raise AirwallexError(r.status_code, d.get("code", "http_error"), d.get("message", str(resp)[:200]))
        return resp


class Airwallex:
    def __init__(self, t: Transport):
        self.t = t
        self.sim = Sim(t)

    # --- payments -------------------------------------------------------------------------
    def create_payment_intent(self, request_id: str, amount: float, currency: str, order_id: str,
                              return_url: str = "https://example.com/onceback/return") -> dict:
        return self.t.request("POST", "/pa/payment_intents/create", {
            "request_id": request_id, "amount": amount, "currency": currency,
            "merchant_order_id": order_id, "return_url": return_url})

    def confirm_payment_intent(self, pid: str, request_id: str, card: dict) -> dict:
        """Server-side confirm (mock only: the real account has no native API access)."""
        return self.t.request("POST", f"/pa/payment_intents/{pid}/confirm", {
            "request_id": request_id, "payment_method": {"type": "card", "card": card}})

    def get_payment_intent(self, pid: str) -> dict:
        return self.t.request("GET", f"/pa/payment_intents/{pid}")

    # --- refunds --------------------------------------------------------------------------
    def create_refund(self, request_id: str, payment_intent_id: str, amount: float, reason: str,
                      metadata: dict | None = None) -> dict:
        body = {"request_id": request_id, "payment_intent_id": payment_intent_id, "amount": amount,
                "reason": reason[:128]}
        if metadata:
            body["metadata"] = metadata
        return self.t.request("POST", "/pa/refunds/create", body)

    def list_refunds(self, payment_intent_id: str) -> list[dict]:
        r = self.t.request("GET", "/pa/refunds", params={"payment_intent_id": payment_intent_id})
        return r.get("items", []) if isinstance(r, dict) else r

    def find_refund(self, payment_intent_id: str, request_id: str) -> dict | None:
        """Recover after a lost response: a replayed request_id answers 400 duplicate_request."""
        return next((x for x in self.list_refunds(payment_intent_id) if x.get("request_id") == request_id), None)

    def get_refund(self, rid: str) -> dict:
        return self.t.request("GET", f"/pa/refunds/{rid}")

    # --- disputes -------------------------------------------------------------------------
    def list_disputes(self, payment_intent_id: str) -> list[dict]:
        r = self.t.request("GET", "/pa/payment_disputes", params={"payment_intent_id": payment_intent_id})
        return r.get("items", []) if isinstance(r, dict) else r

    def get_dispute(self, dispute_id: str) -> dict:
        return self.t.request("GET", f"/pa/payment_disputes/{dispute_id}")

    def accept_dispute(self, dispute_id: str, request_id: str, description: str,
                       reason: str = "VALID_CUSTOMER_DISPUTE") -> dict:
        return self.t.request("POST", f"/pa/payment_disputes/{dispute_id}/accept", {
            "request_id": request_id, "reason": reason, "accepted_by": "onceback",
            "description": description[:500]})

    def challenge_dispute(self, dispute_id: str, request_id: str, file_ids: list[str], description: str,
                          product_description: str = "", product_type: str = "PHYSICAL_GOODS") -> dict:
        return self.t.request("POST", f"/pa/payment_disputes/{dispute_id}/challenge", {
            "request_id": request_id, "challenged_by": "onceback", "reason": "CUSTOMER_REFUNDED",
            "product_type": product_type, "product_description": product_description or description[:100],
            "supporting_documents": {"documents": [
                {"type": "PRIMARY", "file_ids": file_ids, "description": description[:500]}]}})

    def upload_file(self, filename: str, data: bytes, content_type: str, notes: str = "") -> str:
        r = self.t.request("POST", "FILES/files/upload", {"filename": filename, "content_type": content_type,
                                                          "notes": notes, "file": data})
        return r.get("file_id") or r.get("id")

    # --- payouts --------------------------------------------------------------------------
    def verify_account(self, bank_details: dict, entity_type: str = "PERSONAL", method: str = "LOCAL") -> dict:
        return self.t.request("POST", "/beneficiaries/verify_account", {
            "entity_type": entity_type, "transfer_method": method, "bank_details": bank_details})

    def create_beneficiary(self, request_id: str, beneficiary: dict, methods: list[str],
                           nickname: str = "") -> dict:
        body = {"request_id": request_id, "beneficiary": beneficiary, "transfer_methods": methods}
        if nickname:
            body["nickname"] = nickname
        return self.t.request("POST", "/beneficiaries/create", body)

    def balances(self) -> dict[str, float]:
        r = self.t.request("GET", "/balances/current")
        return {b["currency"]: float(b.get("available_amount", 0)) for b in r}

    def quote(self, sell: str, buy: str, buy_amount: float, validity: str = "MIN_1") -> dict:
        return self.t.request("POST", "/fx/quotes/create", {
            "sell_currency": sell, "buy_currency": buy, "buy_amount": buy_amount, "validity": validity})

    def convert(self, request_id: str, sell: str, buy: str, buy_amount: float, quote_id: str | None) -> dict:
        body = {"request_id": request_id, "sell_currency": sell, "buy_currency": buy, "buy_amount": buy_amount}
        if quote_id:
            body["quote_id"] = quote_id
        return self.t.request("POST", "/fx/conversions/create", body)

    def create_transfer(self, request_id: str, beneficiary_id: str, amount: float, currency: str,
                        reference: str, method: str = "LOCAL") -> dict:
        return self.t.request("POST", "/transfers/create", {
            "request_id": request_id, "beneficiary_id": beneficiary_id, "transfer_amount": amount,
            "transfer_currency": currency, "source_currency": currency, "transfer_method": method,
            "reference": reference[:140], "reason": "refund"})

    def get_transfer(self, tid: str) -> dict:
        return self.t.request("GET", f"/transfers/{tid}")


class Sim:
    """Sandbox-only levers. In production these are real-world events (webhooks)."""

    def __init__(self, t: Transport):
        self.t = t

    def dispute(self, payment_intent_id: str, amount: float, reason_code: str = "13.1",
                stage: str = "CHARGEBACK", due_days: int = 10, comment: str = "") -> dict:
        from datetime import datetime, timedelta, timezone
        due = (datetime.now(timezone.utc) + timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        return self.t.request("POST", "/simulation/pa/payment_disputes/create", {
            "payment_intent_id": payment_intent_id, "amount": amount, "reason_code": reason_code,
            "stage": stage, "due_at": due, "comment": comment or "simulated by OnceBack demo"})

    def transfer(self, tid: str, next_status: str) -> dict:
        return self.t.request("POST", f"/simulation/transfers/{tid}/transition", {"next_status": next_status})

    def deposit(self, global_account_id: str, amount: float) -> dict:
        return self.t.request("POST", "/simulation/deposit/create",
                              {"global_account_id": global_account_id, "amount": amount})
