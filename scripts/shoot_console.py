"""Drives the console headlessly through the demo and saves screenshots.
usage: python scripts_shoot.py BASE_URL OUTDIR PREFIX"""
import sys, time
from playwright.sync_api import sync_playwright

base, out, pre = sys.argv[1], sys.argv[2], sys.argv[3]
W, H = 1600, 1000


def shot(pg, name):
    pg.wait_for_timeout(1300)
    pg.screenshot(path=f"{out}/{pre}_{name}.png")
    print("saved", f"{out}/{pre}_{name}.png", flush=True)


def wait_status(pg, cid, statuses, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        st = pg.evaluate(f"fetch('/api/cases/{cid}').then(r=>r.json()).then(j=>j.status)")
        if st in statuses:
            return st
        pg.wait_for_timeout(700)
    raise SystemExit(f"{cid} stuck")


def open_case(pg, cid):
    pg.click(f'[data-case="{cid}"]'); pg.wait_for_timeout(500)


def run(pg, cid):
    open_case(pg, cid); pg.click("#btn-run")


def approve(pg):
    pg.wait_for_selector(".appr .ok", timeout=180000); pg.wait_for_timeout(800)
    pg.click(".appr .ok")


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    pg.goto(base); pg.wait_for_timeout(1500)
    shot(pg, "01_start")
    pg.click("#btn-setup")
    pg.wait_for_function("fetch('/api/state').then(r=>r.json()).then(s=>s.setup.status==='ready')", timeout=300000, polling=1000)
    open_case(pg, "C1"); shot(pg, "02_ready")
    pg.click("#btn-run"); wait_status(pg, "C1", ["done"]); shot(pg, "03_C1_card_refund")
    run(pg, "C2"); wait_status(pg, "C2", ["awaiting_approval"]); shot(pg, "04_C2_approval_card")
    approve(pg); wait_status(pg, "C2", ["done"]); open_case(pg, "C2"); shot(pg, "05_C2_paid_jp_bank")
    run(pg, "C3"); wait_status(pg, "C3", ["done"]); shot(pg, "06_C3_chargeback_race")
    run(pg, "C4"); wait_status(pg, "C4", ["awaiting_approval"]); shot(pg, "07_C4_injection_approval")
    approve(pg); wait_status(pg, "C4", ["done"]); open_case(pg, "C4"); shot(pg, "08_C4_refunded")
    run(pg, "C1-dup"); wait_status(pg, "C1-dup", ["done"]); shot(pg, "09_C1dup_declined")
    run(pg, "C5"); wait_status(pg, "C5", ["awaiting_approval"]); shot(pg, "10_C5_partial_match_approval")
    approve(pg); wait_status(pg, "C5", ["done"]); open_case(pg, "C5"); shot(pg, "11_C5_paid_gb_bank")
    open_case(pg, "C4"); pg.click("#btn-cb")
    pg.wait_for_selector('[data-case="ORD-1004-chargeback"]', timeout=120000)
    wait_status(pg, "ORD-1004-chargeback", ["done"]); open_case(pg, "ORD-1004-chargeback"); shot(pg, "12_C4_chargeback_challenged")
    b.close()
