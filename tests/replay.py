"""Replays real Airwallex sandbox responses recorded on 2026-10-10."""
from __future__ import annotations

import json
import pathlib

from onceback.airwallex import AirwallexError

FIX = pathlib.Path(__file__).parent / "fixtures"
VOLATILE = {"request_id", "due_at"}


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip(x) for x in o]
    return o


class ReplayTransport:
    """Strict: each call must match the next recorded (method, path, params, body-minus-volatile)."""

    def __init__(self, entries: list[dict]):
        self.entries, self.i = entries, 0

    def _next(self, method, path, json_body, params):
        assert self.i < len(self.entries), f"unexpected extra call {method} {path}"
        e = self.entries[self.i]
        self.i += 1
        assert (e["method"], e["path"]) == (method, path), f"call {self.i}: {method} {path} != {e['method']} {e['path']}"
        if e.get("params") is not None or params is not None:
            assert (e.get("params") or {}) == (params or {}), f"call {self.i} params differ"
        if json_body is not None and e.get("json") is not None and not path.startswith("FILES"):
            assert _strip(json_body) == _strip(e["json"]), f"call {self.i} body differs:\n{json_body}\n{e['json']}"
        if e["status"] >= 300:
            r = e["response"] if isinstance(e["response"], dict) else {}
            raise AirwallexError(e["status"], r.get("code", "error"), r.get("message", ""))
        return e["response"]

    def request(self, method, path, json=None, params=None):
        return self._next(method, path, json, params)

