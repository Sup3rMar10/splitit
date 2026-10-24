# Architecture

One FastAPI process, SQLite, and a plain JS client. Rules live in `app/services.py`; the HTTP layer and `seed.py` both call it.

```
 Browser (static/app.js)                         seed.py
   | fetch JSON, session cookie                    |
   v                                               v
+------------------------------------------------------------------+
| FastAPI (main.py)   Origin check, CSP, rate limits, pydantic     |
|   routes: thin, one `require_member()` per group-scoped route    |
|            v                                                     |
| services.py   authz, validation, splits, balances, idempotency,  |
|               audit log, CSV                                     |
|     |-- money.py     integer minor units, Decimal rates,         |
|     |                allocate() largest-remainder                 |
|     `-- simplify.py  greedy min-cash-flow + exact bitmask DP     |
|            v                                                     |
| db.py   SQLite (WAL, FKs on), BEGIN IMMEDIATE write transactions |
+------------------------------------------------------------------+
```

## API (all under `/api`, JSON, cookie session)

| Method | Path | Notes |
|--------|------|-------|
| POST | /register, /login, /logout | rate limited per IP; argon2 hashes; sessions stored as sha256 |
| GET | /me, /currencies | |
| GET, POST | /groups | list mine, create (creator becomes owner) |
| POST | /groups/join | `{code}`; guesses are rate limited per user |
| GET | /groups/{id} | members + invite code |
| GET, POST | /groups/{id}/expenses | POST takes an optional `Idempotency-Key` header |
| DELETE | /groups/{id}/expenses/{eid} | soft delete; creator or group owner |
| GET | /groups/{id}/balances | net per member + the fewest transfers |
| GET, POST | /groups/{id}/settlements | POST: only the payer or recipient; idempotent |
| GET | /groups/{id}/activity | audit log, newest first |
| GET | /groups/{id}/export.csv | one row per expense split, plus settlements |

An expense body: `{description, amount_minor, currency, paid_by, split_type, splits: [{user_id, value}], expense_date?}`. `value` means minor units for `exact`, basis points for `percentage` (1% = 100, must total 10000), a share count for `shares`, and is ignored for `equal`. All integers, strictly typed.

## Authorization
Every group-scoped route calls `require_member(group, user)` first. It returns a 404 for non-members, the same answer as for a group that doesn't exist, so nothing reveals what exists. Child resources are looked up *with* their group (`WHERE e.id = ? AND e.group_id = ?`), so an expense id from another group is not found even if you belong to the group in the URL. Referenced users (payer, split participants, settlement parties) must be members of that group. Integer ids are guessable on purpose; authorization is the defence, not obscurity.

## Money flow for one expense
1. Validate the split inputs by type and turn them into integer weights.
2. Convert the total to the group's currency once: `Decimal`, rounded half up. Store original amount, converted amount and the rate used.
3. `allocate(converted_total, weights, rotation=seq)` returns the per-person shares, which sum exactly to the converted total.
4. Insert the expense, the splits and an audit-log row in one transaction.

Balances are computed from stored integers: paid minus owed, adjusted by settlements. They always sum to zero.

## Idempotent writes
`run_idempotent(user, key, scope, body, write)` runs inside one `BEGIN IMMEDIATE` transaction: look up `(user, key)`; a stored row with the same request hash returns the saved response without writing; a different hash is a 422; otherwise do the write and store the key and response in the same transaction. Keys are per user, and the group membership check runs before any replay.

## Audit trail
`activity` rows are written in the same transaction as the change they describe (who, what, when, entity, detail JSON). SQLite triggers abort any UPDATE or DELETE on the table. Expenses are soft-deleted (`deleted_at`, `deleted_by`) and keep their splits, so history is reconstructible.

## DB schema (SQLite)

```sql
users(id, email UNIQUE NOCASE, name, password_hash, created_at)
sessions(token_hash PK, user_id, expires_at)
groups(id, name, currency, invite_code UNIQUE, created_by, created_at)
members(group_id, user_id, role owner|member, joined_at)  PK(group_id, user_id)
expenses(id, group_id, seq, description, amount_minor>0, currency, rate, base_amount_minor>0,
         paid_by, split_type, expense_date, created_by, created_at, deleted_at, deleted_by)
splits(expense_id, user_id, value, base_share_minor>=0)  PK(expense_id, user_id)
settlements(id, group_id, from_user, to_user, amount_minor>0, created_by, created_at)  CHECK from<>to
activity(id, group_id, actor_id, action, entity_type, entity_id, detail, created_at)  -- append-only triggers
idempotency(user_id, key, request_hash, status, response, created_at)  PK(user_id, key)
```
