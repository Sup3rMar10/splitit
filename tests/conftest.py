import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)  # tests only: argon2 at full cost is slow


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def app(db_path):
    return create_app(db_path, hasher=FAST_HASHER, auth_limit=(10_000, 10_000))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def new_client(app):
    """Factory: every call is a separate browser with its own cookie jar."""
    return lambda: TestClient(app)


def signup(client, name, password="correct-horse-battery"):
    r = client.post("/api/register", json={"email": f"{name.lower()}@example.com", "name": name, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def make_group(client, name="Trip", currency="USD"):
    r = client.post("/api/groups", json={"name": name, "currency": currency})
    assert r.status_code == 201, r.text
    return r.json()


def expense_body(group, split_type="equal", amount=10000, currency=None, paid_by=None, splits=None, **extra):
    members = [m["id"] for m in group["members"]]
    return {
        "description": "Dinner",
        "amount_minor": amount,
        "currency": currency or group["currency"],
        "paid_by": paid_by or members[0],
        "split_type": split_type,
        "splits": splits if splits is not None else [{"user_id": m} for m in members],
        **extra,
    }


def add_expense(client, group, key=None, expect=201, **kw):
    headers = {"Idempotency-Key": key} if key else {}
    r = client.post(f"/api/groups/{group['id']}/expenses", json=expense_body(group, **kw), headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


@pytest.fixture
def trio(new_client):
    """Alice (owner), Bob and Carol in one USD group. Returns clients, users and the group dict."""
    a, b, c = new_client(), new_client(), new_client()
    ua, ub, uc = signup(a, "Alice"), signup(b, "Bob"), signup(c, "Carol")
    g = make_group(a)
    for cl in (b, c):
        assert cl.post("/api/groups/join", json={"code": g["invite_code"]}).status_code == 200
    g = a.get(f"/api/groups/{g['id']}").json()
    return {"a": a, "b": b, "c": c, "users": (ua, ub, uc), "group": g}
