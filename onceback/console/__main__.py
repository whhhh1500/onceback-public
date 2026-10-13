"""python -m onceback.console [--live] [--llm] [--port 8000] [--poll 5]"""
from __future__ import annotations

import argparse
import os

import uvicorn

from ..airwallex import Airwallex, LiveTransport, load_env
from ..demo import REGISTRY
from ..mock import MockTransport
from .app import create_app
from .engine import Engine


def main(argv=None):
    ap = argparse.ArgumentParser(prog="onceback.console")
    ap.add_argument("--live", action="store_true", help="real Airwallex sandbox (.env AWX_*)")
    ap.add_argument("--llm", action="store_true", help="LLM reader (CC1500_* env)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--poll", type=float, default=5.0, help="dispute polling interval in seconds (0 = off)")
    ap.add_argument("--record", help="append redacted API calls (live) to this JSONL")
    a = ap.parse_args(argv)
    load_env()
    t = LiveTransport(record_to=a.record) if a.live else MockTransport(balances={"USD": 5000.0}, bank_registry=REGISTRY)
    engine = Engine(Airwallex(t), live=a.live, use_llm=a.llm, webhook_secret=os.environ.get("AWX_WEBHOOK_SECRET"))
    if a.poll > 0:
        engine.start_poller(a.poll)
    print(f"OnceBack console ({'live sandbox' if a.live else 'mock'}) on http://{a.host}:{a.port}")
    uvicorn.run(create_app(engine), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
