"""Test shopper: pays a sandbox PaymentIntent with a test card through the
Airwallex.js Card element (the PCI-safe client path), driven by headless Chrome.

Used because this sandbox account is not enabled for server-side
("native API") card confirmation. Card data only ever enters Airwallex's iframe.
"""
from __future__ import annotations

import http.server
import functools
import os
import pathlib
import threading
import time

HERE = pathlib.Path(__file__).parent
CHROME = os.environ.get("ONCEBACK_CHROME", "/usr/bin/google-chrome")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep demo output clean
        pass


class Shopper:
    """Serves checkout.html on 127.0.0.1 (Airwallex.js needs an http origin) and drives it."""

    def __init__(self, headless: bool = True):
        from playwright.sync_api import sync_playwright
        handler = functools.partial(_Quiet, directory=str(HERE))
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self._pw = sync_playwright().start()
        kw = {"executable_path": CHROME} if os.path.exists(CHROME) else {}
        self._browser = self._pw.chromium.launch(headless=headless, args=["--no-sandbox"], **kw)

    def close(self):
        self._browser.close(); self._pw.stop(); self._srv.shutdown()

    def pay(self, intent_id: str, client_secret: str, currency: str, card: str = "4035501000000008",
            expiry: str = "12/30", cvc: str = "123", timeout_s: int = 60) -> dict:
        page = self._browser.new_page()
        try:
            page.goto(f"http://127.0.0.1:{self._port}/checkout.html#intent_id={intent_id}"
                      f"&client_secret={client_secret}&currency={currency}")
            page.wait_for_function("window.__state === 'ready' || window.__state === 'error'", timeout=timeout_s * 1000)
            if page.evaluate("window.__state") == "error":
                return page.evaluate("window.__result")
            frame = page.frame_locator("#card iframe")
            frame.locator("input").nth(0).fill(card)
            inputs = frame.locator("input")
            n = inputs.count()
            if n >= 3:
                inputs.nth(1).fill(expiry); inputs.nth(2).fill(cvc)
            time.sleep(0.5)
            page.click("#submit")
            page.wait_for_function("window.__state === 'done' || window.__state === 'error'", timeout=timeout_s * 1000)
            return page.evaluate("window.__result")
        finally:
            page.close()
