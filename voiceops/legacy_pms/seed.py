"""Seed data for the mock PMS. Dates are relative to "today" so the demo never goes stale."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

SEED_SQL = """
DROP TABLE IF EXISTS residents; DROP TABLE IF EXISTS charges; DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS leases; DROP TABLE IF EXISTS renewal_offers; DROP TABLE IF EXISTS renewals;
DROP TABLE IF EXISTS payment_links; DROP TABLE IF EXISTS payment_plans; DROP TABLE IF EXISTS prospects;
DROP TABLE IF EXISTS tour_slots; DROP TABLE IF EXISTS notes; DROP TABLE IF EXISTS idempotency;
CREATE TABLE residents(resident_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, unit TEXT, phone TEXT, email TEXT, tz TEXT, flags TEXT);
CREATE TABLE charges(id INTEGER PRIMARY KEY, resident_id TEXT, code TEXT, description TEXT, amount REAL, due_date TEXT);
CREATE TABLE payments(id INTEGER PRIMARY KEY, resident_id TEXT, amount REAL, method TEXT, created_at TEXT);
CREATE TABLE leases(lease_id TEXT PRIMARY KEY, resident_id TEXT, start_date TEXT, end_date TEXT, rent REAL, renewal_status TEXT);
CREATE TABLE renewal_offers(offer_id TEXT PRIMARY KEY, resident_id TEXT, term_months INTEGER, rent REAL, expires_on TEXT);
CREATE TABLE renewals(renewal_id TEXT PRIMARY KEY, resident_id TEXT, offer_id TEXT, status TEXT, created_at TEXT);
CREATE TABLE payment_links(link_id TEXT PRIMARY KEY, resident_id TEXT, amount REAL, status TEXT, created_at TEXT);
CREATE TABLE payment_plans(plan_id TEXT PRIMARY KEY, resident_id TEXT, installments TEXT, status TEXT, created_at TEXT);
CREATE TABLE prospects(prospect_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, phone TEXT, email TEXT, interest TEXT, status TEXT, last_contact TEXT);
CREATE TABLE tour_slots(slot_id TEXT PRIMARY KEY, starts_at TEXT, kind TEXT, booked_by TEXT);
CREATE TABLE notes(id INTEGER PRIMARY KEY, entity_id TEXT, text TEXT, created_at TEXT);
CREATE TABLE idempotency(key TEXT PRIMARY KEY, body_hash TEXT, response TEXT);
"""

RESIDENTS = [
    # id, first, last, unit, phone, email, tz, flags
    ("R-1001", "Maria", "Lopez", "4B", "+12065550101", "maria.lopez@example.com", "America/Los_Angeles", ""),
    ("R-1002", "James", "Chen", "12A", "+12065550102", "james.chen@example.com", "America/Los_Angeles", ""),
    ("R-1003", "Aisha", "Patel", "7C", "+12065550103", "aisha.patel@example.com", "America/Los_Angeles", ""),
    ("R-1004", "Daniel", "Kim", "2D", "+12065550104", "daniel.kim@example.com", "America/New_York", ""),
    ("R-1005", "Sofia", "Rossi", "9F", "+12065550105", "sofia.rossi@example.com", "America/Los_Angeles", "DISPUTE"),
]


def seed(db: sqlite3.Connection, today: date) -> None:
    first_of_month = today.replace(day=1)
    last_month = (first_of_month - timedelta(days=1)).replace(day=1)
    late_fee_day = first_of_month + timedelta(days=5)
    db.executemany("INSERT INTO residents VALUES (?,?,?,?,?,?,?,?)", RESIDENTS)

    def rent(rid, amount, unpaid_now, late_fee=True):
        db.execute("INSERT INTO charges(resident_id, code, description, amount, due_date) VALUES (?,?,?,?,?)",
                   (rid, "RENT", f"Rent {last_month:%b %Y}", amount, last_month.isoformat()))
        db.execute("INSERT INTO payments(resident_id, amount, method, created_at) VALUES (?,?,?,?)",
                   (rid, amount, "ACH", last_month.isoformat()))
        db.execute("INSERT INTO charges(resident_id, code, description, amount, due_date) VALUES (?,?,?,?,?)",
                   (rid, "RENT", f"Rent {first_of_month:%b %Y}", amount, first_of_month.isoformat()))
        if not unpaid_now:
            db.execute("INSERT INTO payments(resident_id, amount, method, created_at) VALUES (?,?,?,?)",
                       (rid, amount, "ACH", first_of_month.isoformat()))
        elif late_fee and today > late_fee_day:
            db.execute("INSERT INTO charges(resident_id, code, description, amount, due_date) VALUES (?,?,?,?,?)",
                       (rid, "LATE", "Late fee", 75.0, late_fee_day.isoformat()))

    rent("R-1001", 1375.0, unpaid_now=True)
    rent("R-1002", 1820.0, unpaid_now=False)
    rent("R-1003", 2025.0, unpaid_now=True)
    rent("R-1004", 725.0, unpaid_now=True)
    rent("R-1005", 225.0, unpaid_now=True)

    leases = [
        ("L-501", "R-1001", today - timedelta(days=200), today + timedelta(days=165), 1375.0),
        ("L-502", "R-1002", today - timedelta(days=300), today + timedelta(days=65), 1820.0),
        ("L-503", "R-1003", today - timedelta(days=100), today + timedelta(days=265), 2025.0),
        ("L-504", "R-1004", today - timedelta(days=50), today + timedelta(days=315), 725.0),
        ("L-505", "R-1005", today - timedelta(days=330), today + timedelta(days=35), 225.0),
    ]
    for lid, rid, start, end, r in leases:
        db.execute("INSERT INTO leases VALUES (?,?,?,?,?,?)", (lid, rid, start.isoformat(), end.isoformat(), r, "OFFERED" if rid == "R-1002" else "NONE"))
    db.execute("INSERT INTO renewal_offers VALUES (?,?,?,?,?)", ("OF-12", "R-1002", 12, 1895.0, (today + timedelta(days=30)).isoformat()))
    db.execute("INSERT INTO renewal_offers VALUES (?,?,?,?,?)", ("OF-6", "R-1002", 6, 1975.0, (today + timedelta(days=30)).isoformat()))

    db.executemany("INSERT INTO prospects VALUES (?,?,?,?,?,?,?,?)", [
        ("P-2001", "Tyler", "Brooks", "+12065550201", "tyler.brooks@example.com", "2 bedroom, move in December", "NEW",
         (today - timedelta(days=2)).isoformat()),
        ("P-2002", "Emma", "Davis", "+12065550202", "emma.davis@example.com", "1 bedroom", "APPLICATION_STARTED",
         (today - timedelta(days=3)).isoformat()),
    ])
    n = 0
    for d in range(1, 4):
        day = today + timedelta(days=d)
        for hour, kind in ((10, "AGENT_LED"), (14, "SELF_GUIDED")):
            n += 1
            db.execute("INSERT INTO tour_slots VALUES (?,?,?,NULL)", (f"S-{n:02d}", f"{day.isoformat()}T{hour:02d}:00", kind))
    db.commit()
