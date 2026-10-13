"""HTTP layer for the OnceBack console. Binds to 127.0.0.1 by default."""
from __future__ import annotations

import json
import pathlib

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import Engine
from .webhooks import SignatureError, verify

STATIC = pathlib.Path(__file__).parent / "static"
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "cf-connecting-ip", "x-real-ip")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


class Answer(BaseModel):
    hash: str = Field(min_length=8, max_length=64)
    decision: str = Field(pattern="^(approve|reject)$")
    approver: str = Field(default="operator", max_length=40)


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="OnceBack console", docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Through a tunnel/proxy (or from another host) only the signed webhook endpoint answers;
        # the console, its API and the Approve buttons stay loopback-only.
        proxied = any(h in request.headers for h in PROXY_HEADERS)
        host = request.client.host if request.client else ""
        if (proxied or host not in LOOPBACK) and request.url.path != "/webhooks/airwallex":
            return JSONResponse({"error": "console is local-only"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        return engine.state()

    @app.get("/api/cases/{case_id}")
    def case(case_id: str):
        try:
            return engine.case_detail(case_id)
        except KeyError:
            raise HTTPException(404, "no such case")

    @app.post("/api/setup")
    def setup():
        engine.setup()
        return engine.state()["setup"]

    @app.post("/api/cases/{case_id}/run")
    def run(case_id: str):
        if case_id not in engine.cases:
            raise HTTPException(404, "no such case")
        try:
            engine.run_case(case_id)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return engine.case_detail(case_id)

    @app.post("/api/approvals/{case_id}")
    def approve(case_id: str, a: Answer):
        try:
            p = engine.answer(case_id, a.hash, a.decision, a.approver)
        except KeyError:
            raise HTTPException(404, "nothing pending for this case")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return p.public()

    @app.post("/api/orders/{order_id}/chargeback")
    def chargeback(order_id: str):
        if order_id not in engine.orders:
            raise HTTPException(404, "no such order (prepare the demo orders first)")
        d = engine.simulate_chargeback(order_id)
        return {"dispute_id": d["id"], "status": d.get("status")}

    @app.post("/api/poll")
    def poll():
        return engine.poll_once()

    @app.post("/webhooks/airwallex")
    async def webhook(request: Request):
        body = await request.body()
        try:
            verify(engine.webhook_secret, request.headers.get("x-timestamp"), request.headers.get("x-signature"), body)
        except SignatureError as e:
            engine.log("webhook", f"rejected delivery: {e}")
            return JSONResponse({"error": str(e)}, status_code=401)
        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        return engine.handle_event(event)

    return app
