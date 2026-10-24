"""Business rules. The HTTP layer and the seed script both call these functions.

Write functions expect the caller to have opened a transaction (`with tx(db):`)
so a request's checks, writes and audit-log entry commit or roll back together.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import date

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .db import tx
from .money import MAX_AMOUNT_MINOR, Rates, allocate, format_minor
from .simplify import simplify

SESSION_TTL = 7 * 24 * 3600
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid misreads
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def not_found() -> AppError:
    # One answer for "doesn't exist" and "not yours": never confirm a resource exists to an outsider.
    return AppError(404, "Not found")


def _now() -> int:
    return int(time.time())


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --- users and sessions ---------------------------------------------------------


def create_user(db: sqlite3.Connection, hasher: PasswordHasher, email: str, name: str, password: str) -> dict:
    email = email.strip().lower()
    try:
        cur = db.execute(
            "INSERT INTO users(email, name, password_hash, created_at) VALUES(?,?,?,?)",
            (email, name, hasher.hash(password), _now()),
        )
    except sqlite3.IntegrityError:
        raise AppError(409, "That email is already registered") from None
    return {"id": cur.lastrowid, "email": email, "name": name}


# Verified against when the email is unknown, so login takes about the same time either way.
_DUMMY_HASH: str | None = None


def authenticate(db: sqlite3.Connection, hasher: PasswordHasher, email: str, password: str) -> dict:
    global _DUMMY_HASH
    row = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hasher.hash("not-a-real-password")
        try:
            hasher.verify(_DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            pass
        raise AppError(401, "Wrong email or password")
    try:
        hasher.verify(row["password_hash"], password)
    except (VerificationError, InvalidHashError):
        raise AppError(401, "Wrong email or password") from None
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def create_session(db: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
        (_sha(token), user_id, _now() + SESSION_TTL),
    )
    return token


def user_for_token(db: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    row = db.execute(
        "SELECT u.id, u.email, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (_sha(token), _now()),
    ).fetchone()
    return dict(row) if row else None


def delete_session(db: sqlite3.Connection, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),))


# --- audit trail ----------------------------------------------------------------


def log(db, group_id: int, actor_id: int, action: str, entity_type: str, entity_id: int | None, detail: dict) -> None:
    db.execute(
        "INSERT INTO activity(group_id, actor_id, action, entity_type, entity_id, detail, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (group_id, actor_id, action, entity_type, entity_id, json.dumps(detail, sort_keys=True), _now()),
    )


def activity_list(db, group_id: int, limit: int = 100) -> list[dict]:
    rows = db.execute(
        "SELECT a.id, a.action, a.entity_type, a.entity_id, a.detail, a.created_at, u.name AS actor "
        "FROM activity a JOIN users u ON u.id = a.actor_id WHERE a.group_id = ? ORDER BY a.id DESC LIMIT ?",
        (group_id, limit),
    ).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# --- groups ---------------------------------------------------------------------


def require_member(db, group_id: int, user_id: int) -> sqlite3.Row:
    """The authorization gate for everything group-scoped. Non-members get a 404."""
    row = db.execute(
        "SELECT g.id, g.name, g.currency, g.invite_code, g.created_by, m.role "
        "FROM groups g JOIN members m ON m.group_id = g.id WHERE g.id = ? AND m.user_id = ?",
        (group_id, user_id),
    ).fetchone()
    if row is None:
        raise not_found()
    return row


def _new_invite_code(db) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))
        if db.execute("SELECT 1 FROM groups WHERE invite_code = ?", (code,)).fetchone() is None:
            return code
    raise AppError(500, "Could not allocate an invite code")


def create_group(db, rates: Rates, user_id: int, name: str, currency: str) -> dict:
    if not rates.supports(currency):
        raise AppError(422, f"Unsupported currency {currency}")
    now = _now()
    cur = db.execute(
        "INSERT INTO groups(name, currency, invite_code, created_by, created_at) VALUES(?,?,?,?,?)",
        (name, currency, _new_invite_code(db), user_id, now),
    )
    gid = cur.lastrowid
    db.execute("INSERT INTO members(group_id, user_id, role, joined_at) VALUES(?,?,?,?)", (gid, user_id, "owner", now))
    log(db, gid, user_id, "group_created", "group", gid, {"name": name, "currency": currency})
    return group_detail(db, gid)


def join_group(db, user_id: int, code: str) -> dict:
    row = db.execute("SELECT id FROM groups WHERE invite_code = ?", (code.strip().upper(),)).fetchone()
    if row is None:
        raise AppError(404, "Invalid invite code")
    cur = db.execute(
        "INSERT OR IGNORE INTO members(group_id, user_id, role, joined_at) VALUES(?,?,?,?)",
        (row["id"], user_id, "member", _now()),
    )
    if cur.rowcount:
        log(db, row["id"], user_id, "member_joined", "member", user_id, {})
    return group_detail(db, row["id"])


def group_detail(db, group_id: int) -> dict:
    g = db.execute("SELECT id, name, currency, invite_code, created_at FROM groups WHERE id = ?", (group_id,)).fetchone()
    members = db.execute(
        "SELECT u.id, u.name, m.role FROM members m JOIN users u ON u.id = m.user_id "
        "WHERE m.group_id = ? ORDER BY u.id",
        (group_id,),
    ).fetchall()
    return {**dict(g), "members": [dict(m) for m in members]}


def list_groups(db, user_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT g.id, g.name, g.currency FROM groups g JOIN members m ON m.group_id = g.id "
        "WHERE m.user_id = ? ORDER BY g.id",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- expenses -------------------------------------------------------------------


def _member_ids(db, group_id: int) -> set[int]:
    return {r["user_id"] for r in db.execute("SELECT user_id FROM members WHERE group_id = ?", (group_id,))}


def _parse_date(value: str | None) -> str:
    if value is None:
        return date.today().isoformat()
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise AppError(422, "expense_date must be YYYY-MM-DD") from None
    if not 2000 <= d.year <= 2100:
        raise AppError(422, "expense_date out of range")
    return d.isoformat()


def _weights(split_type: str, amount_minor: int, entries: list[tuple[int, int]]) -> list[int]:
    values = [v for _, v in entries]
    if split_type == "equal":
        return [1] * len(entries)
    if split_type == "exact":
        if any(v <= 0 for v in values):
            raise AppError(422, "Exact amounts must be positive; leave out people who owe nothing")
        if sum(values) != amount_minor:
            raise AppError(422, "Exact amounts must add up to the expense total")
        return values
    if split_type == "percentage":
        if any(v <= 0 for v in values):
            raise AppError(422, "Percentages must be positive")
        if sum(values) != 10_000:
            raise AppError(422, "Percentages must add up to 100%")
        return values
    if split_type == "shares":
        if any(v < 1 for v in values):
            raise AppError(422, "Each person needs at least 1 share")
        return values
    raise AppError(422, "Unknown split type")


def add_expense(db, rates: Rates, group_id: int, actor_id: int, data: dict) -> int:
    group = require_member(db, group_id, actor_id)
    members = _member_ids(db, group_id)
    currency = data["currency"]
    if not rates.supports(currency):
        raise AppError(422, f"Unsupported currency {currency}")
    amount = data["amount_minor"]
    if not 0 < amount <= MAX_AMOUNT_MINOR:
        raise AppError(422, "Amount out of range")
    if data["paid_by"] not in members:
        raise AppError(422, "Payer must be a member of the group")
    entries = [(s["user_id"], s.get("value", 1)) for s in data["splits"]]
    ids = [u for u, _ in entries]
    if len(set(ids)) != len(ids):
        raise AppError(422, "Duplicate person in split")
    if not set(ids) <= members:
        raise AppError(422, "Everyone in the split must be a member of the group")
    entries.sort()  # stable order: remainder cents never depend on request ordering
    weights = _weights(data["split_type"], amount, entries)

    base_total, rate = rates.convert(amount, currency, group["currency"])
    if base_total <= 0:
        raise AppError(422, "Amount is too small after currency conversion")
    seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM expenses WHERE group_id = ?", (group_id,)).fetchone()[0]
    shares = allocate(base_total, weights, rotation=seq)

    cur = db.execute(
        "INSERT INTO expenses(group_id, seq, description, amount_minor, currency, rate, base_amount_minor, paid_by, "
        "split_type, expense_date, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (group_id, seq, data["description"], amount, currency, rate, base_total, data["paid_by"],
         data["split_type"], _parse_date(data.get("expense_date")), actor_id, _now()),
    )
    eid = cur.lastrowid
    db.executemany(
        "INSERT INTO splits(expense_id, user_id, value, base_share_minor) VALUES(?,?,?,?)",
        [(eid, u, v, s) for (u, v), s in zip(entries, shares)],
    )
    log(db, group_id, actor_id, "expense_added", "expense", eid, {
        "description": data["description"], "amount_minor": amount, "currency": currency,
        "base_amount_minor": base_total, "paid_by": data["paid_by"], "split_type": data["split_type"],
    })
    return eid


def get_expense(db, group_id: int, expense_id: int) -> dict:
    # Scoped by group_id too: an expense id from another group is "not found", not "forbidden".
    e = db.execute(
        "SELECT e.*, p.name AS paid_by_name FROM expenses e JOIN users p ON p.id = e.paid_by "
        "WHERE e.id = ? AND e.group_id = ? AND e.deleted_at IS NULL",
        (expense_id, group_id),
    ).fetchone()
    if e is None:
        raise not_found()
    splits = db.execute(
        "SELECT s.user_id, u.name, s.value, s.base_share_minor FROM splits s JOIN users u ON u.id = s.user_id "
        "WHERE s.expense_id = ? ORDER BY s.user_id",
        (expense_id,),
    ).fetchall()
    return {
        "id": e["id"], "description": e["description"], "amount_minor": e["amount_minor"],
        "currency": e["currency"], "rate": e["rate"], "base_amount_minor": e["base_amount_minor"],
        "paid_by": e["paid_by"], "paid_by_name": e["paid_by_name"], "split_type": e["split_type"],
        "expense_date": e["expense_date"], "created_by": e["created_by"], "created_at": e["created_at"],
        "splits": [dict(s) for s in splits],
    }


def list_expenses(db, group_id: int) -> list[dict]:
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM expenses WHERE group_id = ? AND deleted_at IS NULL ORDER BY expense_date DESC, id DESC",
        (group_id,),
    )]
    return [get_expense(db, group_id, i) for i in ids]


def delete_expense(db, group_id: int, expense_id: int, actor_id: int) -> None:
    group = require_member(db, group_id, actor_id)
    e = get_expense(db, group_id, expense_id)
    if e["created_by"] != actor_id and group["role"] != "owner":
        raise AppError(403, "Only the person who added it, or the group owner, can delete an expense")
    db.execute("UPDATE expenses SET deleted_at = ?, deleted_by = ? WHERE id = ?", (_now(), actor_id, expense_id))
    log(db, group_id, actor_id, "expense_deleted", "expense", expense_id, {
        "description": e["description"], "base_amount_minor": e["base_amount_minor"],
    })


# --- settlements and balances ---------------------------------------------------


def add_settlement(db, group_id: int, actor_id: int, from_user: int, to_user: int, amount_minor: int) -> int:
    require_member(db, group_id, actor_id)
    members = _member_ids(db, group_id)
    if from_user == to_user:
        raise AppError(422, "Payer and recipient must differ")
    if from_user not in members or to_user not in members:
        raise AppError(422, "Both people must be members of the group")
    if actor_id not in (from_user, to_user):
        raise AppError(403, "You can only record payments you made or received")
    if not 0 < amount_minor <= MAX_AMOUNT_MINOR:
        raise AppError(422, "Amount out of range")
    cur = db.execute(
        "INSERT INTO settlements(group_id, from_user, to_user, amount_minor, created_by, created_at) VALUES(?,?,?,?,?,?)",
        (group_id, from_user, to_user, amount_minor, actor_id, _now()),
    )
    log(db, group_id, actor_id, "settlement_added", "settlement", cur.lastrowid, {
        "from_user": from_user, "to_user": to_user, "amount_minor": amount_minor,
    })
    return cur.lastrowid


def list_settlements(db, group_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT s.id, s.from_user, f.name AS from_name, s.to_user, t.name AS to_name, s.amount_minor, s.created_at "
        "FROM settlements s JOIN users f ON f.id = s.from_user JOIN users t ON t.id = s.to_user "
        "WHERE s.group_id = ? ORDER BY s.id DESC",
        (group_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_net(db, group_id: int) -> dict[int, int]:
    """Net position per member in the group's currency; positive = is owed money. Sums to zero."""
    net = {u: 0 for u in _member_ids(db, group_id)}
    for r in db.execute(
        "SELECT paid_by AS u, SUM(base_amount_minor) AS t FROM expenses "
        "WHERE group_id = ? AND deleted_at IS NULL GROUP BY paid_by", (group_id,)):
        net[r["u"]] += r["t"]
    for r in db.execute(
        "SELECT s.user_id AS u, SUM(s.base_share_minor) AS t FROM splits s JOIN expenses e ON e.id = s.expense_id "
        "WHERE e.group_id = ? AND e.deleted_at IS NULL GROUP BY s.user_id", (group_id,)):
        net[r["u"]] -= r["t"]
    for r in db.execute("SELECT from_user, to_user, amount_minor FROM settlements WHERE group_id = ?", (group_id,)):
        net[r["from_user"]] += r["amount_minor"]
        net[r["to_user"]] -= r["amount_minor"]
    return net


def balances_view(db, group_id: int, currency: str) -> dict:
    net = compute_net(db, group_id)
    names = {r["id"]: r["name"] for r in db.execute(
        "SELECT u.id, u.name FROM members m JOIN users u ON u.id = m.user_id WHERE m.group_id = ?", (group_id,))}
    transfers = simplify(net)
    return {
        "currency": currency,
        "balances": [{"user_id": u, "name": names[u], "net_minor": net[u]} for u in sorted(net)],
        "transfers": [
            {"from_user": f, "from_name": names[f], "to_user": t, "to_name": names[t], "amount_minor": a}
            for f, t, a in transfers
        ],
    }


# --- idempotent writes ----------------------------------------------------------


def run_idempotent(db, user_id: int, key: str | None, scope: str, body: dict, write):
    """Run `write() -> (status, payload)` at most once per (user, key).

    A retry with the same key and the same request returns the stored response
    without writing again. The same key with a different request is a client
    bug and is rejected. The key row is written in the same transaction as the
    data, so a crash can't leave one without the other.
    Returns (status, payload, replayed).
    """
    if key is not None and not KEY_RE.match(key):
        raise AppError(422, "Idempotency-Key must be 8-64 characters of A-Z a-z 0-9 _ -")
    request_hash = _sha(json.dumps({"scope": scope, "body": body}, sort_keys=True, separators=(",", ":")))
    with tx(db):
        if key is not None:
            row = db.execute("SELECT * FROM idempotency WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise AppError(422, "Idempotency-Key was already used with a different request")
                return row["status"], json.loads(row["response"]), True
        status, payload = write()
        if key is not None:
            db.execute(
                "INSERT INTO idempotency(user_id, key, request_hash, status, response, created_at) VALUES(?,?,?,?,?,?)",
                (user_id, key, request_hash, status, json.dumps(payload), _now()),
            )
        return status, payload, False


# --- CSV export -----------------------------------------------------------------

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_text_cell(value: str) -> str:
    """Spreadsheets run cells that start with = + - @ as formulas. Defuse user text with a leading quote."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def export_csv(db, group_id: int, currency: str) -> str:
    import csv
    import io

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["type", "id", "date", "description", "paid_by", "currency", "amount", "rate_to_group_currency",
                f"amount_{currency}", "member", f"share_{currency}"])
    for e in reversed(list_expenses(db, group_id)):
        for s in e["splits"]:
            w.writerow(["expense", e["id"], e["expense_date"], csv_text_cell(e["description"]),
                        csv_text_cell(e["paid_by_name"]), e["currency"], format_minor(e["amount_minor"], e["currency"]),
                        e["rate"], format_minor(e["base_amount_minor"], currency), csv_text_cell(s["name"]),
                        format_minor(s["base_share_minor"], currency)])
    for s in reversed(list_settlements(db, group_id)):
        w.writerow(["settlement", s["id"], time.strftime("%Y-%m-%d", time.gmtime(s["created_at"])), "settlement",
                    csv_text_cell(s["from_name"]), currency, format_minor(s["amount_minor"], currency), "1",
                    format_minor(s["amount_minor"], currency), csv_text_cell(s["to_name"]),
                    format_minor(s["amount_minor"], currency)])
    return out.getvalue()
