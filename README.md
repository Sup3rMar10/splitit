# Splitit
💸 Friends don't let friends do math at dinner.

Groups share expenses and settle up with the fewest payments. Sign up, make a group, invite friends with a code, add expenses (equal, exact amounts, percentages or shares) in any of a handful of currencies, and the dashboard shows who owes whom, a balance chart, and a Settle button. Everything is logged, and you can export to CSV.

## Run it 🏃
Python 3.10+ (tested on 3.13).

```
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python seed.py                    # optional: a fake "Goa trip" group with 5 people
uvicorn app.main:create_app --factory --port 8000
```

Open http://127.0.0.1:8000. After seeding, log in as `aarav@example.com` (or meera, rohan, ishita, kabir `@example.com`); the demo password is the `PASSWORD` constant at the top of `seed.py`. It's a local demo account, so don't reuse it anywhere.

Tests: `pip install -r requirements-dev.txt && pytest` (53 tests, about 25 seconds).

Env vars: `SPLITIT_DB` (default `splitit.db`), `SPLITIT_SECURE_COOKIES=1` (set when serving over HTTPS).

## How the money works
- **Integer minor units only.** 12.34 USD is stored as `1234`. No float touches an amount: the API rejects `12.5`, `"12"` and `true` for amounts, rates are parsed as `Decimal`, and the browser parses typed amounts with a regex, not `parseFloat`. JPY has no minor unit, KWD has three; that's handled per currency.
- **Splits are one function.** Every split type becomes integer weights, then `allocate()` hands out the total by largest remainder, so the parts always sum to exactly the total. Leftover cents go to the biggest fractional remainders; ties rotate by a per-group counter so the extra cent isn't always the same person's. No randomness: same input, same output.
- **Multi-currency** uses `data/rates.json` (static demo rates, not live). An expense converts once at creation, rounded half up, and the converted amount and rate are stored, so editing the rates file never rewrites history.
- **Fewest payments:** net balances, then a greedy min-cash-flow, then (for up to 14 people) an exact search that finds the true minimum. See `docs/STEPS.md` for why both.

## What was actually tested
- 53 automated tests: property tests on the allocator (3,000 random cases), the simplifier checked against a brute-force oracle, every split type, conversions, balances always summing to zero over 150 random expenses, idempotency (including 10 concurrent retries), the append-only log, CSV formula injection, CSRF, rate limits, and an IDOR suite (non-members hitting every route, cross-group expense ids, id enumeration, forged payers and participants, replayed idempotency keys, revoked sessions).
- I **mutation-tested the IDOR suite**: I deliberately removed a membership check from a route, un-scoped an expense lookup to its group, and made the membership check ignore the user. Each flaw failed the tests aimed at it (3, 1 and 6 tests respectively), then I restored the code.
- Clicked through in Chrome against the seeded database: login, dashboard and chart, adding an exact-split expense (live "still to assign" hint, client-side decimal check), and the Settle button.

## Honest disclaimer
**This is a test app only. It is a local prototype that has never been deployed and is not production-ready. Don't put real financial data or real passwords in it.** Known gaps:
- Rates are static. There's no live FX feed and no historical rates.
- A settlement is a record that someone says they paid; nothing verifies money moved. Anyone involved can record one.
- Expenses can be deleted but not edited (delete and re-add), which keeps the audit trail simple.
- Registering with a taken email says so (it reveals which emails exist). Login itself doesn't.
- Sessions last 7 days and the cookie is not `Secure` unless you set the env var. Rate limits are in memory, per process.
- Idempotency keys are never expired. The audit log is append-only via SQLite triggers, but it isn't tamper-evident (no hash chain).
- SQLite and one process. No email verification, password reset, or removing members from a group.

## Things that went wrong while building this 🙃
- My first draft of the "remainder is request-order independent" test used a group with one member, so it tested nothing. And one balance assertion was written so it could never fail. I rewrote both with hand-computed expected values.
- The balance chart clipped its biggest label off the edge of the SVG ("26118.93 IN"). Amounts now have their own column.
- Naming a path-parameter helper `Path` collided with `pathlib.Path` in FastAPI, a fun way to lose five minutes.

Split the bill, keep the friends. 🫡
