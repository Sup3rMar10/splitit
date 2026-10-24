"""Seed a fake "Goa trip" group: python seed.py

Goes through the same service functions as the API, so the demo data obeys the
same rules (integer money, deterministic remainders, audit log entries).
Demo login for everyone: <name>@example.com / goa-trip-2026
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from argon2 import PasswordHasher

from app import services as svc
from app.db import connect, init_db, tx
from app.money import Rates, format_minor

ROOT = Path(__file__).resolve().parent
PASSWORD = "goa-trip-2026"
PEOPLE = ["Aarav", "Meera", "Rohan", "Ishita", "Kabir"]


def seed(db_path: str) -> dict:
    init_db(db_path)
    db = connect(db_path)
    rates = Rates.load(ROOT / "data" / "rates.json")
    hasher = PasswordHasher()
    if db.execute("SELECT 1 FROM users WHERE email = ?", ("aarav@example.com",)).fetchone():
        raise SystemExit("Already seeded (aarav@example.com exists). Delete the database file to start over.")

    with tx(db):
        ids = {n: svc.create_user(db, hasher, f"{n.lower()}@example.com", n, PASSWORD)["id"] for n in PEOPLE}
        group = svc.create_group(db, rates, ids["Aarav"], "Goa trip", "INR")
        for n in PEOPLE[1:]:
            svc.join_group(db, ids[n], group["invite_code"])
        gid = group["id"]

        def expense(by, desc, amount, currency, split_type, splits, day, actor=None):
            return svc.add_expense(db, rates, gid, ids[actor or by], {
                "description": desc, "amount_minor": amount, "currency": currency, "paid_by": ids[by],
                "split_type": split_type, "expense_date": f"2026-10-{day:02d}",
                "splits": [{"user_id": ids[n], **({} if v is None else {"value": v})} for n, v in splits],
            })

        everyone = [(n, None) for n in PEOPLE]
        expense("Aarav", "Flights (booked in USD)", 48_000, "USD", "equal", everyone, 10)            # multi-currency
        expense("Meera", "Beach villa, 4 nights", 4_800_000, "INR", "exact",
                [("Aarav", 1_200_000), ("Meera", 1_200_000), ("Rohan", 900_000), ("Ishita", 900_000), ("Kabir", 600_000)], 12)
        expense("Rohan", "Scooter rentals", 600_000, "INR", "shares",
                [("Aarav", 1), ("Rohan", 2), ("Ishita", 1), ("Kabir", 1)], 13)                        # Rohan rode two
        expense("Ishita", "Shack dinner, Baga", 784_533, "INR", "equal", everyone, 13)               # odd paise to split
        expense("Meera", "Dolphin boat trip", 240_000, "INR", "percentage",
                [("Meera", 3000), ("Ishita", 3000), ("Kabir", 4000)], 14)
        expense("Kabir", "Airport coffee", 8_500, "AED", "equal", [("Kabir", None), ("Aarav", None), ("Rohan", None)], 15)
        oops = expense("Meera", "Duplicate hotel deposit", 1_500_000, "INR", "equal", everyone, 15)
        svc.delete_expense(db, gid, oops, ids["Meera"])                                              # shows up in the audit log
        svc.add_settlement(db, gid, ids["Kabir"], ids["Kabir"], ids["Meera"], 200_000)               # Kabir pays Meera 2,000

    view = svc.balances_view(db, gid, "INR")
    return {"group": group, "ids": ids, "view": view}


def main() -> None:
    db_path = os.environ.get("SPLITIT_DB", str(ROOT / "splitit.db"))
    out = seed(db_path)
    g, view = out["group"], out["view"]
    print(f"Seeded '{g['name']}' (group {g['id']}, invite code {g['invite_code']}) in {db_path}")
    print(f"Log in as any of {', '.join(n.lower() + '@example.com' for n in PEOPLE)} with password {PASSWORD}\n")
    print("Balances (INR):")
    for b in view["balances"]:
        print(f"  {b['name']:<8} {format_minor(b['net_minor'], 'INR'):>12}")
    print("Fewest payments to settle:")
    for t in view["transfers"]:
        print(f"  {t['from_name']} -> {t['to_name']}  {format_minor(t['amount_minor'], 'INR')}")


if __name__ == "__main__":
    sys.exit(main())
