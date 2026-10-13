"""Console API (mock sandbox) and webhook signature tests."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from onceback.airwallex import Airwallex
from onceback.console.app import create_app
from onceback.console.engine import Engine
from onceback.console.webhooks import SignatureError, sign, verify
from onceback.demo import REGISTRY
from onceback.mock import MockTransport

SECRET = "whsec_test_123"


@pytest.fixture
def env():
    t = MockTransport(balances={"USD": 5000.0}, bank_registry=REGISTRY)
    eng = Engine(Airwallex(t), live=False, webhook_secret=SECRET, approval_timeout_s=10)
    client = TestClient(create_app(eng))
    assert client.post("/api/setup").status_code == 200
    wait(lambda: client.get("/api/state").json()["setup"]["status"] == "ready")
    yield t, eng, client
    eng.stop()


def wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def case(client, cid):
    return client.get(f"/api/cases/{cid}").json()


def run_and_wait(client, cid, status=("done",)):
    assert client.post(f"/api/cases/{cid}/run").status_code == 200
    wait(lambda: case(client, cid)["status"] in status)
    return case(client, cid)


def test_index_and_state(env):
    t, eng, client = env
    assert "OnceBack" in client.get("/").text
    s = client.get("/api/state").json()
    assert s["mode"] == "mock" and s["setup"]["status"] == "ready" and s["webhook_configured"]
    assert [c["id"] for c in s["cases"]][:3] == ["C1", "C2", "C3"]
    assert len(s["ledger"]) == 5 and all(l["returned"] == 0 for l in s["ledger"])
    assert client.get("/api/cases/nope").status_code == 404


def test_card_refund_runs_without_approval(env):
    t, eng, client = env
    c = run_and_wait(client, "C1")
    assert c["outcome"] == "REFUNDED_TO_CARD"
    stages = [s["stage"] for s in c["steps"]]
    assert stages[0] == "GOAL" and "PLAN" in stages and stages[-1] == "ACT"
    assert client.post("/api/approvals/C1", json={"hash": "x" * 16, "decision": "approve"}).status_code == 404


def test_approval_card_shows_terms_and_binds_hash(env):
    t, eng, client = env
    assert client.post("/api/cases/C4/run").status_code == 200
    wait(lambda: client.get("/api/state").json()["approvals"])
    a = client.get("/api/state").json()["approvals"][0]
    assert a["case_id"] == "C4" and a["currency"] == "GBP" and a["binding"]["payment_intent_id"]
    assert case(client, "C4")["status"] == "awaiting_approval"
    r = client.post("/api/approvals/C4", json={"hash": "0" * 16, "decision": "approve"})
    assert r.status_code == 409 and not t.refunds                        # wrong hash moves nothing
    r = client.post("/api/approvals/C4", json={"hash": a["hash"], "decision": "approve"})
    assert r.status_code == 200
    wait(lambda: case(client, "C4")["status"] == "done")
    assert case(client, "C4")["outcome"] == "REFUNDED_TO_CARD"
    r = client.post("/api/approvals/C4", json={"hash": a["hash"], "decision": "approve"})
    assert r.status_code == 409                                         # a card can't be reused


def test_reject_moves_nothing(env):
    t, eng, client = env
    client.post("/api/cases/C4/run")
    wait(lambda: client.get("/api/state").json()["approvals"])
    a = client.get("/api/state").json()["approvals"][0]
    assert client.post("/api/approvals/C4", json={"hash": a["hash"], "decision": "reject"}).status_code == 200
    wait(lambda: case(client, "C4")["status"] == "done")
    assert case(client, "C4")["outcome"] == "REJECTED_BY_APPROVER"
    assert not t.refunds and all(l["returned"] == 0 for l in client.get("/api/state").json()["ledger"])


def test_chargeback_race_is_accepted_not_refunded(env):
    t, eng, client = env
    c = run_and_wait(client, "C3")
    assert c["outcome"] == "ACCEPT_DISPUTE" and not t.refunds
    assert client.post("/api/poll").json()["opened"] == 0                # the race dispute is already handled


def test_simulated_chargeback_found_by_poller(env):
    t, eng, client = env
    run_and_wait(client, "C4", status=("awaiting_approval",))
    a = client.get("/api/state").json()["approvals"][0]
    client.post("/api/approvals/C4", json={"hash": a["hash"], "decision": "approve"})
    wait(lambda: case(client, "C4")["status"] == "done")
    assert client.post("/api/orders/ORD-1004/chargeback").status_code == 200
    assert client.post("/api/poll").json()["opened"] == 1
    wait(lambda: case(client, "ORD-1004-chargeback")["status"] == "done")
    cb = case(client, "ORD-1004-chargeback")
    assert cb["outcome"] == "CHALLENGE_DISPUTE" and cb["source"] == "poll"
    assert client.post("/api/poll").json()["opened"] == 0


def test_run_before_setup_is_refused():
    client = TestClient(create_app(Engine(Airwallex(MockTransport()), live=False)))
    assert client.post("/api/cases/C1/run").status_code == 409


def post_event(client, ev, secret=SECRET, ts=None):
    body = json.dumps(ev).encode()
    ts = ts or str(int(time.time() * 1000))
    sig = sign(secret, ts, body) if secret else "00"
    return client.post("/webhooks/airwallex", content=body, headers={"x-timestamp": ts, "x-signature": sig})


def test_signed_webhook_accepted_once(env):
    t, eng, client = env
    ev = {"id": "evt_1", "name": "refund.accepted", "data": {"object": {"id": "rfd_1", "status": "ACCEPTED"}}}
    assert post_event(client, ev).json()["status"] == "noted"
    assert post_event(client, ev).json()["status"] == "duplicate"
    assert post_event(client, ev, secret="wrong").status_code == 401


def test_tampered_webhook_body_rejected(env):
    t, eng, client = env
    good = json.dumps({"id": "evt_2", "name": "refund.accepted", "data": {"object": {}}}).encode()
    ts = str(int(time.time() * 1000))
    sig = sign(SECRET, ts, good)
    bad = good.replace(b"refund.accepted", b"payment_dispute.requires_response")
    r = client.post("/webhooks/airwallex", content=bad, headers={"x-timestamp": ts, "x-signature": sig})
    assert r.status_code == 401


def test_webhook_dispute_event_opens_case(env):
    t, eng, client = env
    client.post("/api/cases/C1/run")
    wait(lambda: case(client, "C1")["status"] == "done")
    o = eng.orders["ORD-1001"]
    d = eng.simulate_chargeback("ORD-1001")
    ev = {"id": "evt_d1", "name": "payment_dispute.requires_response",
          "data": {"object": {"id": d["id"], "payment_intent_id": o.payment_intent_id, "status": "REQUIRES_RESPONSE"}}}
    assert post_event(client, ev).json()["status"] == "case_opened"
    wait(lambda: case(client, "ORD-1001-chargeback")["status"] == "done")
    assert case(client, "ORD-1001-chargeback")["source"] == "webhook"
    assert client.post("/api/poll").json()["opened"] == 0                # poller won't double-handle it


def test_webhook_without_configured_secret_rejects_everything():
    t = MockTransport()
    client = TestClient(create_app(Engine(Airwallex(t), live=False, webhook_secret=None)))
    assert post_event(client, {"id": "e", "name": "x"}).status_code == 401


def test_verify_unit():
    ts = "1791650000000"
    s = sign("k", ts, b"{}")
    verify("k", ts, s, b"{}", now_ms=int(ts))
    verify("k", ts, s.upper(), b"{}", now_ms=int(ts))
    for args in [("k", ts, "00", b"{}"), ("k", None, s, b"{}"), ("k", "abc", s, b"{}"), (None, ts, s, b"{}")]:
        with pytest.raises(SignatureError):
            verify(*args, now_ms=int(ts))
    with pytest.raises(SignatureError):
        verify("k", ts, s, b"{}", now_ms=int(ts) + 6 * 60 * 1000)


def test_tunnelled_requests_only_reach_the_webhook(env):
    t, eng, client = env
    fwd = {"x-forwarded-for": "203.0.113.9"}
    assert client.get("/api/state", headers=fwd).status_code == 403
    assert client.post("/api/approvals/C4", headers=fwd, json={"hash": "x" * 16, "decision": "approve"}).status_code == 403
    assert client.get("/", headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 403
    ev = {"id": "evt_t1", "name": "refund.accepted", "data": {"object": {"id": "rfd_1"}}}
    body = json.dumps(ev).encode()
    ts = str(int(time.time() * 1000))
    r = client.post("/webhooks/airwallex", content=body,
                    headers={**fwd, "x-timestamp": ts, "x-signature": sign(SECRET, ts, body)})
    assert r.status_code == 200
