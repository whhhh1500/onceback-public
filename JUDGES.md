# OnceBack: a 5-minute guide for judges

## 1. Watch (4:47)
[docs/OnceBack_demo.mp4](docs/OnceBack_demo.mp4): all seven cases run against the real Airwallex sandbox in the operator console.
Live transcripts: [docs/LIVE_RUN.md](docs/LIVE_RUN.md).

## 2. Run it offline (no credentials)
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # includes replay of a recorded live sandbox run
.venv/bin/python -m onceback --mock    # all 7 cases in the terminal
.venv/bin/python -m onceback.console   # console at http://127.0.0.1:8000
```
Live sandbox mode needs your own Airwallex sandbox keys: copy `.env.example` to `.env`, then `python -m onceback --live`.

## 3. What to look for
| Claim | Case | Where in the code |
|---|---|---|
| Money amounts are decided in code, not by the model | C4 (prompt injection asks for £6,400; policy pays 576) | `onceback/policy.py` → `decide`, `refund_amount` |
| Card rail closed → bank payout with name check + FX | C2 (JPY), C5 (GBP, PARTIAL_MATCH) | `onceback/policy.py` → `choose_rail`, `decide_payout`; `onceback/agent.py` → `_bank_payout`, `_ensure_currency` |
| Never refund *and* lose a chargeback | C3, C4-later | `onceback/agent.py` → `_switch_to_dispute`, `on_dispute`; `onceback/policy.py` → `decide_on_dispute` |
| One refund per payment, idempotent request ids | C1-dup | `onceback/ledger.py` |
| Human approval bound to exact terms by hash | C2, C4, C5 | `onceback/policy.py` → `binding_hash`; console approval endpoint |
| Every Airwallex call in one place | all | `onceback/airwallex.py` (sandbox-only levers isolated in `Sim`) |

## 4. Scope
Sandbox only, synthetic data, no real money. Sandbox limitations and workarounds are listed in the README
("What the sandbox can't do").
