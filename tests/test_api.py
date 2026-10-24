import random
import sqlite3
import threading

import pytest

from app import services as svc
from app.db import connect, tx
from app.money import Rates

from conftest import add_expense, expense_body, make_group, signup


def shares(expense):
    return {s["name"]: s["base_share_minor"] for s in expense["splits"]}


# --- auth -------------------------------------------------------------------------


def test_register_login_logout_and_cookie_flags(client):
    r = client.post("/api/register", json={"email": "A@Example.com", "name": "A", "password": "correct-horse-battery"})
    assert r.status_code == 201 and r.json()["email"] == "a@example.com"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/me").json()["name"] == "A"
    assert client.post("/api/logout").status_code == 204
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"email": "a@example.com", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"email": "ghost@example.com", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"email": "A@EXAMPLE.COM", "password": "correct-horse-battery"}).status_code == 200


def test_register_validation_and_duplicates(client):
    bad = [
        {"email": "x", "name": "A", "password": "correct-horse-battery"},
        {"email": "a@b.co", "name": "", "password": "correct-horse-battery"},
        {"email": "a@b.co", "name": "A", "password": "short"},
        {"email": "a@b.co", "name": "A", "password": "correct-horse-battery", "is_admin": True},
    ]
    for body in bad:
        assert client.post("/api/register", json=body).status_code == 422
    signup(client, "Dup")
    r = client.post("/api/register", json={"email": "dup@example.com", "name": "D2", "password": "correct-horse-battery"})
    assert r.status_code == 409


def test_session_token_is_stored_hashed(client, db_path):
    signup(client, "Hashy")
    token = client.cookies.get("session")
    rows = connect(db_path).execute("SELECT token_hash FROM sessions").fetchall()
    assert rows and all(token not in r["token_hash"] and len(r["token_hash"]) == 64 for r in rows)


def test_auth_endpoints_are_rate_limited(db_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import FAST_HASHER

    c = TestClient(create_app(db_path, hasher=FAST_HASHER, auth_limit=(3, 0.0001)))
    codes = [c.post("/api/login", json={"email": "a@b.co", "password": "x"}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_every_group_route_requires_login(client):
    for method, path in [("get", "/api/groups"), ("post", "/api/groups"), ("get", "/api/groups/1"),
                         ("get", "/api/groups/1/expenses"), ("get", "/api/groups/1/balances"),
                         ("get", "/api/groups/1/activity"), ("get", "/api/groups/1/export.csv"),
                         ("get", "/api/currencies"), ("post", "/api/groups/join")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_security_headers_and_csrf_origin_check(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    body = {"email": "csrf@example.com", "name": "C", "password": "correct-horse-battery"}
    assert client.post("/api/register", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/register", json=body, headers={"Origin": "http://testserver"}).status_code == 201


# --- groups -----------------------------------------------------------------------


def test_create_group_join_by_code_and_membership(new_client):
    a, b = new_client(), new_client()
    signup(a, "Alice")
    ub = signup(b, "Bob")
    g = make_group(a, "Goa", "INR")
    assert len(g["invite_code"]) == 8 and g["members"][0]["role"] == "owner"
    assert b.post("/api/groups/join", json={"code": "ZZZZZZZZ"}).status_code == 404
    r = b.post("/api/groups/join", json={"code": g["invite_code"].lower()})  # codes are case-insensitive
    assert r.status_code == 200 and {m["id"] for m in r.json()["members"]} >= {ub["id"]}
    assert b.post("/api/groups/join", json={"code": g["invite_code"]}).status_code == 200  # joining twice is harmless
    assert len(b.get(f"/api/groups/{g['id']}").json()["members"]) == 2
    assert [x["name"] for x in b.get("/api/groups").json()] == ["Goa"]
    assert a.post("/api/groups", json={"name": "X", "currency": "ZZZ"}).status_code == 422


def test_join_code_guessing_is_rate_limited(db_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import FAST_HASHER

    c = TestClient(create_app(db_path, hasher=FAST_HASHER, auth_limit=(3, 0.0001)))
    signup(c, "Guesser")
    codes = [c.post("/api/groups/join", json={"code": f"AAAAAAA{i}"}).status_code for i in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]


# --- splitting --------------------------------------------------------------------


def test_equal_split_distributes_remainder_and_rotates(trio):
    a, g = trio["a"], trio["group"]
    first = add_expense(a, g)  # 100.00 / 3
    assert sorted(shares(first).values()) == [3333, 3333, 3334] and sum(shares(first).values()) == 10000
    extra_first = [n for n, v in shares(first).items() if v == 3334]
    second = add_expense(a, g)
    extra_second = [n for n, v in shares(second).items() if v == 3334]
    assert extra_first != extra_second  # the odd cent moves on to someone else
    third = add_expense(a, g)
    assert len({tuple(extra_first), tuple(extra_second), tuple([n for n, v in shares(third).items() if v == 3334])}) == 3


def test_remainder_does_not_depend_on_request_order(new_client):
    # Two identical groups; the same 3-way split is requested with the people listed in opposite orders.
    # Remainder cents are assigned from sorted user ids, so each person must get the same share either way.
    a, b = new_client(), new_client()
    signup(a, "Alice")
    signup(b, "Bob")
    g1, g2 = make_group(a, "One"), make_group(a, "Two")
    for g in (g1, g2):
        b.post("/api/groups/join", json={"code": g["invite_code"]})
    g1, g2 = (a.get(f"/api/groups/{g['id']}").json() for g in (g1, g2))
    ids = [m["id"] for m in g1["members"]]
    fwd = add_expense(a, g1, amount=101, splits=[{"user_id": i} for i in ids])
    rev = add_expense(a, g2, amount=101, splits=[{"user_id": i} for i in reversed(ids)])
    assert shares(fwd) == shares(rev) and sorted(shares(fwd).values()) == [50, 51]


def test_exact_percentage_and_shares(trio):
    a, g = trio["a"], trio["group"]
    ua, ub, uc = (u["id"] for u in trio["users"])
    ex = add_expense(a, g, split_type="exact", amount=9000,
                     splits=[{"user_id": ua, "value": 5000}, {"user_id": ub, "value": 3000}, {"user_id": uc, "value": 1000}])
    assert shares(ex) == {"Alice": 5000, "Bob": 3000, "Carol": 1000}
    pc = add_expense(a, g, split_type="percentage", amount=10000,
                     splits=[{"user_id": ua, "value": 3333}, {"user_id": ub, "value": 3333}, {"user_id": uc, "value": 3334}])
    assert sum(shares(pc).values()) == 10000 and sorted(shares(pc).values()) == [3333, 3333, 3334]
    sh = add_expense(a, g, split_type="shares", amount=1000,
                     splits=[{"user_id": ua, "value": 1}, {"user_id": ub, "value": 2}, {"user_id": uc, "value": 2}])
    assert shares(sh) == {"Alice": 200, "Bob": 400, "Carol": 400}
    only_two = add_expense(a, g, amount=500, splits=[{"user_id": ua}, {"user_id": ub}])
    assert set(shares(only_two)) == {"Alice", "Bob"} and sum(shares(only_two).values()) == 500


def test_invalid_expenses_are_rejected(trio, new_client):
    a, g = trio["a"], trio["group"]
    ua, ub, uc = (u["id"] for u in trio["users"])
    stranger = new_client()
    us = signup(stranger, "Stranger")
    url = f"/api/groups/{g['id']}/expenses"
    cases = {
        "exact sum mismatch": expense_body(g, "exact", 1000, splits=[{"user_id": ua, "value": 400}, {"user_id": ub, "value": 500}]),
        "exact zero entry": expense_body(g, "exact", 1000, splits=[{"user_id": ua, "value": 1000}, {"user_id": ub, "value": 0}]),
        "percent not 100": expense_body(g, "percentage", 1000, splits=[{"user_id": ua, "value": 5000}, {"user_id": ub, "value": 4000}]),
        "zero shares": expense_body(g, "shares", 1000, splits=[{"user_id": ua, "value": 0}, {"user_id": ub, "value": 1}]),
        "float amount": {**expense_body(g), "amount_minor": 12.5},
        "string amount": {**expense_body(g), "amount_minor": "1000"},
        "bool amount": {**expense_body(g), "amount_minor": True},
        "zero amount": expense_body(g, amount=0),
        "negative amount": expense_body(g, amount=-5),
        "huge amount": expense_body(g, amount=10**15),
        "unknown currency": expense_body(g, currency="ZZZ"),
        "lowercase currency": expense_body(g, currency="usd"),
        "unknown split type": {**expense_body(g), "split_type": "random"},
        "duplicate person": expense_body(g, splits=[{"user_id": ua}, {"user_id": ua}]),
        "empty splits": expense_body(g, splits=[]),
        "stranger in split": expense_body(g, splits=[{"user_id": ua}, {"user_id": us["id"]}]),
        "stranger pays": expense_body(g, paid_by=us["id"]),
        "extra field": {**expense_body(g), "base_amount_minor": 1},
        "empty description": {**expense_body(g), "description": "  "},
        "bad date": {**expense_body(g), "expense_date": "31/12/2026"},
    }
    for label, body in cases.items():
        assert a.post(url, json=body).status_code == 422, label
    assert a.get(url).json() == []  # nothing leaked through


def test_multi_currency_converts_once_and_snapshots_the_rate(trio):
    a, g = trio["a"], trio["group"]
    e = add_expense(a, g, amount=100_000, currency="JPY", splits=[{"user_id": u["id"]} for u in trio["users"]])
    assert e["currency"] == "JPY" and e["amount_minor"] == 100_000
    assert e["base_amount_minor"] == 66_890 // 1 and sum(shares(e).values()) == e["base_amount_minor"]  # 668.90 USD
    assert e["rate"].startswith("0.0066889")
    inr = make_group(a, "Goa", "INR")
    e2 = add_expense(a, inr, amount=1001, currency="USD", splits=[{"user_id": inr["members"][0]["id"]}])
    assert e2["base_amount_minor"] == 83_333  # 833.33 INR


def test_balances_transfers_and_settle_up(trio):
    a, b, c, g = trio["a"], trio["b"], trio["c"], trio["group"]
    ua, ub, uc = (u["id"] for u in trio["users"])
    add_expense(a, g, amount=9000, paid_by=ua)                       # Alice pays 90, each owes 30
    add_expense(b, g, amount=3000, paid_by=ub, splits=[{"user_id": ua}, {"user_id": uc}])  # Bob pays 30 for Alice+Carol
    bal = a.get(f"/api/groups/{g['id']}/balances").json()
    net = {x["name"]: x["net_minor"] for x in bal["balances"]}
    # Expense 1: Alice paid 90.00, 30.00 each.  Expense 2: Bob paid 30.00 for Alice+Carol, 15.00 each.
    assert net == {"Alice": 4500, "Bob": 0, "Carol": -4500}
    assert [(t["from_name"], t["to_name"], t["amount_minor"]) for t in bal["transfers"]] == [("Carol", "Alice", 4500)]
    # Everyone pays what the simplifier says -> everyone ends at zero
    for t in bal["transfers"]:
        who = {ua: a, ub: b, uc: c}[t["from_user"]]
        r = who.post(f"/api/groups/{g['id']}/settlements",
                     json={"from_user": t["from_user"], "to_user": t["to_user"], "amount_minor": t["amount_minor"]})
        assert r.status_code == 201
    after = a.get(f"/api/groups/{g['id']}/balances").json()
    assert all(x["net_minor"] == 0 for x in after["balances"]) and after["transfers"] == []


def test_settlement_rules(trio):
    a, b, c, g = trio["a"], trio["b"], trio["c"], trio["group"]
    ua, ub, uc = (u["id"] for u in trio["users"])
    url = f"/api/groups/{g['id']}/settlements"
    assert c.post(url, json={"from_user": ua, "to_user": ub, "amount_minor": 100}).status_code == 403  # third party
    assert a.post(url, json={"from_user": ua, "to_user": ua, "amount_minor": 100}).status_code == 422
    assert a.post(url, json={"from_user": ua, "to_user": 9999, "amount_minor": 100}).status_code == 422
    assert a.post(url, json={"from_user": ua, "to_user": ub, "amount_minor": 0}).status_code == 422
    assert a.post(url, json={"from_user": ua, "to_user": ub, "amount_minor": 1.5}).status_code == 422
    assert b.post(url, json={"from_user": ua, "to_user": ub, "amount_minor": 100}).status_code == 201  # recipient may record


def test_balances_always_sum_to_zero_across_random_expenses(db_path, app):
    rates = Rates.load("data/rates.json")
    db = connect(db_path)
    hasher = __import__("conftest").FAST_HASHER
    with tx(db):
        users = [svc.create_user(db, hasher, f"u{i}@x.co", f"U{i}", "correct-horse-battery")["id"] for i in range(5)]
        g = svc.create_group(db, rates, users[0], "Random", "EUR")
        for u in users[1:]:
            svc.join_group(db, u, g["invite_code"])
    rng = random.Random(42)
    with tx(db):
        for _ in range(150):
            kind = rng.choice(["equal", "exact", "percentage", "shares"])
            people = rng.sample(users, rng.randint(1, 5))
            amount = rng.randint(1, 5_000_000)
            if kind == "equal":
                splits = [{"user_id": u} for u in people]
            elif kind == "shares":
                splits = [{"user_id": u, "value": rng.randint(1, 9)} for u in people]
            else:
                total = amount if kind == "exact" else 10_000
                cuts = sorted(rng.sample(range(1, total), len(people) - 1)) if len(people) > 1 else []
                parts = [b - a for a, b in zip([0] + cuts, cuts + [total])]
                splits = [{"user_id": u, "value": p} for u, p in zip(people, parts)]
            svc.add_expense(db, rates, g["id"], rng.choice(users), {
                "description": "r", "amount_minor": amount, "currency": rng.choice(rates.currencies()),
                "paid_by": rng.choice(users), "split_type": kind, "splits": splits})
    assert sum(svc.compute_net(db, g["id"]).values()) == 0
    bad = db.execute("SELECT e.id FROM expenses e WHERE e.base_amount_minor <> "
                     "(SELECT SUM(base_share_minor) FROM splits s WHERE s.expense_id = e.id)").fetchall()
    assert bad == []


def test_delete_expense_permissions_and_effect(trio):
    a, b, c, g = trio["a"], trio["b"], trio["c"], trio["group"]
    e = add_expense(b, g, amount=3000)  # Bob created it; Alice is group owner
    url = f"/api/groups/{g['id']}/expenses/{e['id']}"
    assert c.delete(url).status_code == 403
    assert a.delete(url).status_code == 204  # owner may delete
    assert a.delete(url).status_code == 404
    assert a.get(f"/api/groups/{g['id']}/expenses").json() == []
    assert all(x["net_minor"] == 0 for x in a.get(f"/api/groups/{g['id']}/balances").json()["balances"])
    e2 = add_expense(c, g, amount=500)
    assert c.delete(f"/api/groups/{g['id']}/expenses/{e2['id']}").status_code == 204  # creator may delete


# --- idempotent writes ------------------------------------------------------------


def test_same_key_same_request_is_applied_once(trio):
    a, g = trio["a"], trio["group"]
    url = f"/api/groups/{g['id']}/expenses"
    body = expense_body(g)
    r1 = a.post(url, json=body, headers={"Idempotency-Key": "retry-key-0001"})
    r2 = a.post(url, json=body, headers={"Idempotency-Key": "retry-key-0001"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json() and r2.headers["idempotent-replay"] == "true"
    assert "idempotent-replay" not in r1.headers
    assert len(a.get(url).json()) == 1
    acts = [x for x in a.get(f"/api/groups/{g['id']}/activity").json() if x["action"] == "expense_added"]
    assert len(acts) == 1  # the audit trail didn't double-count either


def test_without_a_key_retries_do_duplicate(trio):
    a, g = trio["a"], trio["group"]
    add_expense(a, g)
    add_expense(a, g)
    assert len(a.get(f"/api/groups/{g['id']}/expenses").json()) == 2


def test_key_reuse_with_a_different_request_is_rejected(trio):
    a, g = trio["a"], trio["group"]
    url = f"/api/groups/{g['id']}/expenses"
    assert a.post(url, json=expense_body(g, amount=1000), headers={"Idempotency-Key": "reuse-key-0001"}).status_code == 201
    r = a.post(url, json=expense_body(g, amount=2000), headers={"Idempotency-Key": "reuse-key-0001"})
    assert r.status_code == 422 and "different request" in r.json()["detail"]
    assert len(a.get(url).json()) == 1


def test_idempotency_keys_are_scoped_per_user_and_validated(trio):
    a, b, g = trio["a"], trio["b"], trio["group"]
    url = f"/api/groups/{g['id']}/expenses"
    assert a.post(url, json=expense_body(g), headers={"Idempotency-Key": "shared-key-0001"}).status_code == 201
    r = b.post(url, json=expense_body(g), headers={"Idempotency-Key": "shared-key-0001"})
    assert r.status_code == 201 and "idempotent-replay" not in r.headers  # Bob's key space is his own
    assert len(a.get(url).json()) == 2
    assert a.post(url, json=expense_body(g), headers={"Idempotency-Key": "short"}).status_code == 422
    assert a.post(url, json=expense_body(g), headers={"Idempotency-Key": "bad key with spaces!!"}).status_code == 422


def test_failed_requests_do_not_burn_the_key(trio):
    a, g = trio["a"], trio["group"]
    url = f"/api/groups/{g['id']}/expenses"
    bad = expense_body(g, "exact", 1000, splits=[{"user_id": g["members"][0]["id"], "value": 1}])
    assert a.post(url, json=bad, headers={"Idempotency-Key": "fix-and-retry-1"}).status_code == 422
    good = expense_body(g, amount=1000)
    assert a.post(url, json=good, headers={"Idempotency-Key": "fix-and-retry-1"}).status_code == 201


def test_settlements_are_idempotent_too(trio):
    a, b, g = trio["a"], trio["b"], trio["group"]
    ua, ub = trio["users"][0]["id"], trio["users"][1]["id"]
    url = f"/api/groups/{g['id']}/settlements"
    body = {"from_user": ub, "to_user": ua, "amount_minor": 700}
    for _ in range(3):
        assert b.post(url, json=body, headers={"Idempotency-Key": "settle-once-01"}).status_code == 201
    assert len(a.get(url).json()) == 1


def test_concurrent_retries_with_one_key_write_exactly_once(db_path, app):
    rates = Rates.load("data/rates.json")
    setup = connect(db_path)
    hasher = __import__("conftest").FAST_HASHER
    with tx(setup):
        uid = svc.create_user(setup, hasher, "race@x.co", "Race", "correct-horse-battery")["id"]
        g = svc.create_group(setup, rates, uid, "Race", "USD")
    data = {"description": "x", "amount_minor": 1234, "currency": "USD", "paid_by": uid,
            "split_type": "equal", "splits": [{"user_id": uid, "value": 1}]}
    results, errors = [], []

    def attempt():
        db = connect(db_path)  # each request gets its own connection, like the real app
        try:
            def write():
                eid = svc.add_expense(db, rates, g["id"], uid, data)
                return 201, {"id": eid}
            results.append(svc.run_idempotent(db, uid, "race-key-00001", "POST /x", data, write))
        except Exception as e:  # pragma: no cover - would fail the test below
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len({r[1]["id"] for r in results}) == 1
    assert sum(1 for r in results if not r[2]) == 1  # exactly one real write, nine replays
    assert setup.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 1


# --- audit trail ------------------------------------------------------------------


def test_activity_records_who_did_what_including_deletes(trio):
    a, b, g = trio["a"], trio["b"], trio["group"]
    e = add_expense(b, g, amount=4200)
    b.delete(f"/api/groups/{g['id']}/expenses/{e['id']}")
    ua, ub = trio["users"][0]["id"], trio["users"][1]["id"]
    b.post(f"/api/groups/{g['id']}/settlements", json={"from_user": ub, "to_user": ua, "amount_minor": 100})
    log = a.get(f"/api/groups/{g['id']}/activity").json()
    actions = [(x["action"], x["actor"]) for x in reversed(log)]
    assert actions == [("group_created", "Alice"), ("member_joined", "Bob"), ("member_joined", "Carol"),
                       ("expense_added", "Bob"), ("expense_deleted", "Bob"), ("settlement_added", "Bob")]
    added = next(x for x in log if x["action"] == "expense_added")
    assert added["detail"]["amount_minor"] == 4200 and added["detail"]["description"] == "Dinner"


def test_activity_log_is_append_only_at_the_database_level(trio, db_path):
    add_expense(trio["a"], trio["group"])
    db = connect(db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE activity SET action = 'nothing happened'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM activity")
    assert db.execute("SELECT COUNT(*) FROM activity").fetchone()[0] >= 4


def test_deleted_expenses_stay_in_the_database(trio, db_path):
    e = add_expense(trio["a"], trio["group"])
    trio["a"].delete(f"/api/groups/{trio['group']['id']}/expenses/{e['id']}")
    row = connect(db_path).execute("SELECT deleted_at, deleted_by FROM expenses WHERE id = ?", (e["id"],)).fetchone()
    assert row["deleted_at"] is not None and row["deleted_by"] == trio["users"][0]["id"]


# --- CSV --------------------------------------------------------------------------


def test_csv_export_content_and_formula_injection_defence(trio):
    a, g = trio["a"], trio["group"]
    add_expense(a, g, amount=10000, description="=HYPERLINK(\"http://evil.example\",\"click\")")
    add_expense(a, g, amount=500, description="+1+1")
    add_expense(a, g, amount=700, description="Plain lunch, with comma")
    r = a.get(f"/api/groups/{g['id']}/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    import csv, io
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:4] == ["type", "id", "date", "description"]
    descs = {row[3] for row in rows[1:]}
    assert "'=HYPERLINK(\"http://evil.example\",\"click\")" in descs and "'+1+1" in descs
    assert "Plain lunch, with comma" in descs  # commas survive via quoting
    assert not any(c.startswith(("=", "+", "-", "@")) for row in rows[1:] for c in (row[3], row[4], row[9]))
    lunch = [row for row in rows if row[3].startswith("Plain")]
    assert sum(int(x[10].replace(".", "")) for x in lunch) == 700


def test_csv_amounts_use_each_currencys_minor_units(trio):
    a, g = trio["a"], trio["group"]
    add_expense(a, g, amount=100_000, currency="JPY", splits=[{"user_id": trio["users"][0]["id"]}])
    import csv, io
    row = list(csv.reader(io.StringIO(a.get(f"/api/groups/{g['id']}/export.csv").text)))[1]
    assert row[5] == "JPY" and row[6] == "100000" and row[8] == "668.90"


# --- IDOR -------------------------------------------------------------------------


@pytest.fixture
def two_tenants(new_client):
    """Alice owns group A with data in it. Mallory is a separate user with his own group M."""
    alice, mallory = new_client(), new_client()
    ua, um = signup(alice, "Alice"), signup(mallory, "Mallory")
    ga, gm = make_group(alice, "Alice's trip"), make_group(mallory, "Mallory's trip")
    ea = add_expense(alice, ga, amount=12345, description="Secret dinner")
    return {"alice": alice, "mallory": mallory, "ua": ua, "um": um, "ga": ga, "gm": gm, "ea": ea}


def test_idor_non_member_cannot_read_any_group_resource(two_tenants):
    m, ga = two_tenants["mallory"], two_tenants["ga"]
    gid = ga["id"]
    for path in ["", "/expenses", "/balances", "/settlements", "/activity", "/export.csv"]:
        r = m.get(f"/api/groups/{gid}{path}")
        assert r.status_code == 404, path
        assert "Secret dinner" not in r.text and ga["invite_code"] not in r.text, path


def test_idor_non_member_cannot_write_to_a_group(two_tenants):
    m, ga, ua, um, ea = (two_tenants[k] for k in ("mallory", "ga", "ua", "um", "ea"))
    gid = ga["id"]
    inject = {**expense_body(ga), "paid_by": um["id"], "splits": [{"user_id": um["id"]}]}
    assert m.post(f"/api/groups/{gid}/expenses", json=inject).status_code == 404
    assert m.post(f"/api/groups/{gid}/settlements",
                  json={"from_user": um["id"], "to_user": ua["id"], "amount_minor": 100}).status_code == 404
    assert m.delete(f"/api/groups/{gid}/expenses/{ea['id']}").status_code == 404
    seen = two_tenants["alice"].get(f"/api/groups/{gid}/expenses").json()
    assert [e["id"] for e in seen] == [ea["id"]]  # untouched
    assert len(two_tenants["alice"].get(f"/api/groups/{gid}/activity").json()) == 2  # group_created + expense_added


def test_idor_expense_id_from_another_group_is_not_reachable_through_your_own(two_tenants):
    m, ea, gm, ga = two_tenants["mallory"], two_tenants["ea"], two_tenants["gm"], two_tenants["ga"]
    # Mallory IS a member of gm, so the group check passes; the expense must still be scoped to that group.
    assert m.delete(f"/api/groups/{gm['id']}/expenses/{ea['id']}").status_code == 404
    assert two_tenants["alice"].get(f"/api/groups/{ga['id']}/expenses").json()[0]["id"] == ea["id"]


def test_idor_cannot_pull_foreign_users_into_your_group(two_tenants):
    m, gm, ua = two_tenants["mallory"], two_tenants["gm"], two_tenants["ua"]
    body = {**expense_body(gm), "splits": [{"user_id": ua["id"]}]}
    assert m.post(f"/api/groups/{gm['id']}/expenses", json=body).status_code == 422
    body = {**expense_body(gm), "paid_by": ua["id"]}
    assert m.post(f"/api/groups/{gm['id']}/expenses", json=body).status_code == 422


def test_idor_sequential_id_enumeration_finds_only_your_own_group(two_tenants):
    m = two_tenants["mallory"]
    reachable = [i for i in range(1, 60) if m.get(f"/api/groups/{i}").status_code == 200]
    assert reachable == [two_tenants["gm"]["id"]]
    for i in range(1, 60):
        if i != two_tenants["gm"]["id"]:
            for suffix in ("/expenses", "/balances", "/activity"):
                assert m.get(f"/api/groups/{i}{suffix}").status_code == 404


def test_idor_absent_and_forbidden_look_identical(two_tenants):
    m, ga = two_tenants["mallory"], two_tenants["ga"]
    real, ghost = m.get(f"/api/groups/{ga['id']}/expenses"), m.get("/api/groups/999999/expenses")
    assert (real.status_code, real.json()) == (ghost.status_code, ghost.json()) == (404, {"detail": "Not found"})


def test_idor_membership_does_not_leak_through_the_group_list_or_invite_code(two_tenants):
    m = two_tenants["mallory"]
    assert [g["name"] for g in m.get("/api/groups").json()] == ["Mallory's trip"]
    assert two_tenants["ga"]["invite_code"] not in m.get(f"/api/groups/{two_tenants['gm']['id']}").text


def test_idor_idempotency_replay_cannot_cross_users_or_groups(two_tenants):
    alice, m, ga, gm = two_tenants["alice"], two_tenants["mallory"], two_tenants["ga"], two_tenants["gm"]
    hdr = {"Idempotency-Key": "cross-user-key-1"}
    ra = alice.post(f"/api/groups/{ga['id']}/expenses", json=expense_body(ga, description="Alice only"), headers=hdr)
    assert ra.status_code == 201
    # Mallory replaying Alice's key against Alice's group: still a 404 (membership checked before any replay)
    assert m.post(f"/api/groups/{ga['id']}/expenses", json=expense_body(ga, description="Alice only"), headers=hdr).status_code == 404
    # ...and against his own group he gets his own fresh write, never Alice's stored response.
    rm = m.post(f"/api/groups/{gm['id']}/expenses", json=expense_body(gm, description="Alice only"), headers=hdr)
    assert rm.status_code == 201 and rm.json()["id"] != ra.json()["id"] and "idempotent-replay" not in rm.headers


def test_idor_logged_out_session_is_dead(two_tenants):
    alice, ga = two_tenants["alice"], two_tenants["ga"]
    token = alice.cookies.get("session")
    alice.post("/api/logout")
    alice.cookies.set("session", token)
    assert alice.get(f"/api/groups/{ga['id']}").status_code == 401  # server-side revocation, not just cookie removal


def test_malformed_ids_are_client_errors_not_500s(trio):
    a = trio["a"]
    for gid in ("0", "-1", "99999999999999999999999999", "abc", "1.5"):
        assert a.get(f"/api/groups/{gid}").status_code == 422, gid
    gid = trio["group"]["id"]
    assert a.delete(f"/api/groups/{gid}/expenses/99999999999999999999999999").status_code == 422
