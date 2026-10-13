# Build status (2026-10-13, UTC+8)

Deadline: 2026-11-14 02:29 UTC+8. Idea published on HackerEarth (Sai's Team 2).

## Done
- Agent, policy, ledger, Airwallex client, evidence reader, mock + live sandbox modes.
- 58/58 pytest pass (incl. replay of recorded live sandbox run; race guard: accept-before-refund, challenge-after-refund, existing open dispute, lost refund response).
- Live sandbox verified 2026-10-10 (docs/LIVE_RUN.md): refunds, payouts (verify_account, FX, transfers), dispute simulate/accept/challenge.
- Operator console (FastAPI) with dispute polling and webhook endpoint; VIDEO_SCRIPT.md written.
- README: test count fixed, transcript link fixed, MIT LICENSE added (ready for public mirror).

## Left before 11-14
1. Demo video (follow docs/VIDEO_SCRIPT.md; record `--mock` run + console; one live run optional).
2. Clean public submit repo: done (fresh history; RESEARCH.md, APPLICATION.md and .env excluded).
3. Optional hardening: webhook signature check against a real sandbox `payment_dispute.*` event (LIVE_RUN.md gap #5).
4. HackerEarth final build submission (repo link + video + description).

## Next parent actions
- Ask user to confirm MIT license and which docs go public.
- Record video (or have user record) from VIDEO_SCRIPT.md.
- Create public repo from a squashed copy, then user submits on HackerEarth.
