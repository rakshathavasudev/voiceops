"""Mock legacy property-management system (the system of record).

It deliberately behaves like an old vendor API rather than a friendly one:
  * OAuth2 client-credentials tokens that expire after a few minutes
  * PascalCase fields, money as strings, dates as MM/DD/YYYY
  * Idempotency-Key required on every write, replayed responses for repeats
  * Fault injection: 503s, slow responses that blow client timeouts, and the nasty
    "write committed but the response was lost" case that only idempotency survives

Run standalone with `uvicorn voiceops.legacy_pms.app:app --port 8001`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from voiceops import clock
from voiceops.legacy_pms.seed import SEED_SQL, seed

TOKEN_TTL_SECONDS = 300
CLIENTS = {"voiceops-agent": "demo-secret"}


def _fmt_date(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _money(v: float) -> str:
    return f"{v:.2f}"


class PMS:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.tokens: dict[str, float] = {}
        self.faults = {"failure_rate": 0.0, "lost_response_rate": 0.0, "slow_rate": 0.0, "slow_ms": 4000}
        self.rng = random.Random(7)
        self.request_count = 0
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.db.executescript(SEED_SQL)
            seed(self.db, clock.now().date())
            self.tokens.clear()
            self.request_count = 0

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid


def create_app(pms: PMS | None = None) -> FastAPI:
    pms = pms or PMS()
    app = FastAPI(title="Legacy PMS (mock)")
    app.state.pms = pms

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        pms.request_count += 1
        if request.url.path.startswith("/api/"):
            f = pms.faults
            if pms.rng.random() < f["slow_rate"]:
                await asyncio.sleep(f["slow_ms"] / 1000)
            if pms.rng.random() < f["failure_rate"]:
                return JSONResponse({"ErrorCode": "SVC_UNAVAILABLE", "Message": "Service temporarily unavailable"}, 503)
            response = await call_next(request)
            if request.method == "POST" and response.status_code < 300 and pms.rng.random() < f["lost_response_rate"]:
                # The write is committed, but the caller never learns that.
                return JSONResponse({"ErrorCode": "GATEWAY_TIMEOUT", "Message": "Upstream timeout"}, 504)
            return response
        return await call_next(request)

    def auth(authorization: str = Header(default="")) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        exp = pms.tokens.get(token)
        if not exp or exp < time.time():
            raise HTTPException(401, {"ErrorCode": "INVALID_TOKEN", "Message": "Token missing or expired"})

    def idempotent(request_key: str | None, body: dict, fn):
        if not request_key:
            raise HTTPException(400, {"ErrorCode": "IDEMPOTENCY_KEY_REQUIRED", "Message": "Idempotency-Key header is required"})
        body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with pms.lock:
            row = pms.q("SELECT body_hash, response FROM idempotency WHERE key=?", request_key)
            if row:
                if row[0]["body_hash"] != body_hash:
                    raise HTTPException(422, {"ErrorCode": "IDEMPOTENCY_CONFLICT", "Message": "Key reused with a different body"})
                return JSONResponse(json.loads(row[0]["response"]), headers={"Idempotent-Replay": "true"})
            result = fn()
            pms.x("INSERT INTO idempotency(key, body_hash, response) VALUES (?,?,?)", request_key, body_hash, json.dumps(result))
            return result

    def resident_or_404(rid: str) -> sqlite3.Row:
        rows = pms.q("SELECT * FROM residents WHERE resident_id=?", rid)
        if not rows:
            raise HTTPException(404, {"ErrorCode": "NOT_FOUND", "Message": f"Resident {rid} not found"})
        return rows[0]

    def resident_json(r: sqlite3.Row) -> dict:
        return {
            "ResidentID": r["resident_id"], "FirstName": r["first_name"], "LastName": r["last_name"],
            "UnitID": r["unit"], "Phone": r["phone"], "Email": r["email"], "TimeZone": r["tz"],
            "Flags": [f for f in (r["flags"] or "").split(",") if f],
        }

    @app.post("/oauth/token")
    def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)):
        if grant_type != "client_credentials" or CLIENTS.get(client_id) != client_secret:
            raise HTTPException(401, {"ErrorCode": "INVALID_CLIENT"})
        tok = secrets.token_hex(16)
        pms.tokens[tok] = time.time() + TOKEN_TTL_SECONDS
        return {"access_token": tok, "token_type": "Bearer", "expires_in": TOKEN_TTL_SECONDS}

    @app.get("/api/v1/residents", dependencies=[Depends(auth)])
    def find_residents(phone: str | None = None, unit: str | None = None, email: str | None = None):
        rows = pms.q(
            "SELECT * FROM residents WHERE (?1 IS NULL OR phone=?1) AND (?2 IS NULL OR upper(unit)=upper(?2)) AND (?3 IS NULL OR lower(email)=lower(?3))",
            phone, unit, email,
        )
        return {"Residents": [resident_json(r) for r in rows]}

    @app.get("/api/v1/residents/{rid}", dependencies=[Depends(auth)])
    def get_resident(rid: str):
        return resident_json(resident_or_404(rid))

    @app.get("/api/v1/residents/{rid}/ledger", dependencies=[Depends(auth)])
    def ledger(rid: str):
        resident_or_404(rid)
        charges = pms.q("SELECT * FROM charges WHERE resident_id=? ORDER BY due_date", rid)
        paid = pms.q("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE resident_id=?", rid)[0]["s"]
        total = sum(c["amount"] for c in charges)
        balance = round(total - paid, 2)
        today = clock.now().date()
        oldest_unpaid = None
        running = paid
        for c in charges:
            running -= c["amount"]
            if running < 0:
                oldest_unpaid = date.fromisoformat(c["due_date"])
                break
        dpd = max(0, (today - oldest_unpaid).days) if oldest_unpaid and balance > 0 else 0
        plans = pms.q("SELECT plan_id FROM payment_plans WHERE resident_id=? AND status='ACTIVE'", rid)
        return {
            "ResidentID": rid,
            "BalanceDue": _money(balance),
            "DaysPastDue": str(dpd),
            "ActivePaymentPlan": plans[0]["plan_id"] if plans else None,
            "Charges": [{"ChargeCode": c["code"], "Description": c["description"], "Amount": _money(c["amount"]),
                         "DueDate": _fmt_date(date.fromisoformat(c["due_date"]))} for c in charges],
        }

    @app.get("/api/v1/residents/{rid}/lease", dependencies=[Depends(auth)])
    def lease(rid: str):
        resident_or_404(rid)
        row = pms.q("SELECT * FROM leases WHERE resident_id=?", rid)[0]
        return {"LeaseID": row["lease_id"], "StartDate": _fmt_date(date.fromisoformat(row["start_date"])),
                "EndDate": _fmt_date(date.fromisoformat(row["end_date"])), "MonthlyRent": _money(row["rent"]),
                "RenewalStatus": row["renewal_status"]}

    @app.get("/api/v1/residents/{rid}/renewal-offers", dependencies=[Depends(auth)])
    def renewal_offers(rid: str):
        resident_or_404(rid)
        rows = pms.q("SELECT * FROM renewal_offers WHERE resident_id=?", rid)
        return {"Offers": [{"OfferID": r["offer_id"], "TermMonths": str(r["term_months"]), "MonthlyRent": _money(r["rent"]),
                            "ExpiresOn": _fmt_date(date.fromisoformat(r["expires_on"]))} for r in rows]}

    @app.post("/api/v1/payment-links", dependencies=[Depends(auth)])
    async def payment_link(request: Request, idempotency_key: str | None = Header(default=None)):
        body = await request.json()

        def create():
            resident_or_404(body["ResidentID"])
            link_id = "PL-" + secrets.token_hex(4).upper()
            pms.x("INSERT INTO payment_links(link_id, resident_id, amount, status, created_at) VALUES (?,?,?,?,?)",
                  link_id, body["ResidentID"], float(body["Amount"]), "OPEN", clock.now().isoformat())
            return {"LinkID": link_id, "Url": f"https://pay.example-pms.com/l/{link_id}", "Status": "OPEN"}

        return idempotent(idempotency_key, body, create)

    @app.post("/api/v1/payment-links/{link_id}/complete")
    def complete_payment_link(link_id: str):
        """Demo hook standing in for the payment processor's webhook."""
        rows = pms.q("SELECT * FROM payment_links WHERE link_id=?", link_id)
        if not rows:
            raise HTTPException(404)
        link = rows[0]
        if link["status"] == "OPEN":
            pms.x("UPDATE payment_links SET status='PAID' WHERE link_id=?", link_id)
            pms.x("INSERT INTO payments(resident_id, amount, method, created_at) VALUES (?,?,?,?)",
                  link["resident_id"], link["amount"], "card", clock.now().isoformat())
        return {"LinkID": link_id, "Status": "PAID"}

    @app.post("/api/v1/payment-plans", dependencies=[Depends(auth)])
    async def payment_plan(request: Request, idempotency_key: str | None = Header(default=None)):
        body = await request.json()

        def create():
            resident_or_404(body["ResidentID"])
            if pms.q("SELECT 1 FROM payment_plans WHERE resident_id=? AND status='ACTIVE'", body["ResidentID"]):
                raise HTTPException(409, {"ErrorCode": "PLAN_EXISTS", "Message": "Resident already has an active plan"})
            plan_id = "PP-" + secrets.token_hex(4).upper()
            pms.x("INSERT INTO payment_plans(plan_id, resident_id, installments, status, created_at) VALUES (?,?,?,?,?)",
                  plan_id, body["ResidentID"], json.dumps(body["Installments"]), "ACTIVE", clock.now().isoformat())
            return {"PlanID": plan_id, "Status": "ACTIVE"}

        return idempotent(idempotency_key, body, create)

    @app.post("/api/v1/lease-renewals", dependencies=[Depends(auth)])
    async def lease_renewal(request: Request, idempotency_key: str | None = Header(default=None)):
        body = await request.json()

        def create():
            offer = pms.q("SELECT * FROM renewal_offers WHERE offer_id=? AND resident_id=?", body["OfferID"], body["ResidentID"])
            if not offer:
                raise HTTPException(404, {"ErrorCode": "OFFER_NOT_FOUND"})
            rid = "RN-" + secrets.token_hex(4).upper()
            pms.x("INSERT INTO renewals(renewal_id, resident_id, offer_id, status, created_at) VALUES (?,?,?,?,?)",
                  rid, body["ResidentID"], body["OfferID"], "PENDING_SIGNATURE", clock.now().isoformat())
            pms.x("UPDATE leases SET renewal_status='PENDING_SIGNATURE' WHERE resident_id=?", body["ResidentID"])
            return {"RenewalID": rid, "Status": "PENDING_SIGNATURE"}

        return idempotent(idempotency_key, body, create)

    @app.get("/api/v1/prospects", dependencies=[Depends(auth)])
    def prospects(phone: str | None = None, email: str | None = None):
        rows = pms.q("SELECT * FROM prospects WHERE (?1 IS NULL OR phone=?1) AND (?2 IS NULL OR lower(email)=lower(?2))", phone, email)
        return {"Prospects": [{"ProspectID": r["prospect_id"], "FirstName": r["first_name"], "LastName": r["last_name"],
                               "Phone": r["phone"], "Email": r["email"], "Interest": r["interest"], "Status": r["status"],
                               "LastContact": r["last_contact"]} for r in rows]}

    @app.get("/api/v1/prospects/{pid}", dependencies=[Depends(auth)])
    def get_prospect(pid: str):
        found = [p for p in prospects(None, None)["Prospects"] if p["ProspectID"] == pid]
        if not found:
            raise HTTPException(404, {"ErrorCode": "NOT_FOUND", "Message": f"Prospect {pid} not found"})
        return found[0]

    @app.get("/api/v1/tour-slots", dependencies=[Depends(auth)])
    def tour_slots():
        rows = pms.q("SELECT * FROM tour_slots WHERE booked_by IS NULL ORDER BY starts_at")
        return {"Slots": [{"SlotID": r["slot_id"], "StartsAt": r["starts_at"], "Type": r["kind"]} for r in rows]}

    @app.post("/api/v1/tours", dependencies=[Depends(auth)])
    async def book_tour(request: Request, idempotency_key: str | None = Header(default=None)):
        body = await request.json()

        def create():
            slot = pms.q("SELECT * FROM tour_slots WHERE slot_id=?", body["SlotID"])
            if not slot or slot[0]["booked_by"]:
                raise HTTPException(409, {"ErrorCode": "SLOT_UNAVAILABLE"})
            pms.x("UPDATE tour_slots SET booked_by=? WHERE slot_id=?", body["ProspectID"], body["SlotID"])
            pms.x("UPDATE prospects SET status='TOUR_BOOKED' WHERE prospect_id=?", body["ProspectID"])
            return {"TourID": "T-" + body["SlotID"], "Status": "BOOKED", "StartsAt": slot[0]["starts_at"]}

        return idempotent(idempotency_key, body, create)

    @app.post("/api/v1/activity-notes", dependencies=[Depends(auth)])
    async def note(request: Request, idempotency_key: str | None = Header(default=None)):
        body = await request.json()

        def create():
            nid = pms.x("INSERT INTO notes(entity_id, text, created_at) VALUES (?,?,?)",
                        body["EntityID"], body["Text"][:4000], clock.now().isoformat())
            return {"NoteID": f"N-{nid}"}

        return idempotent(idempotency_key, body, create)

    # ---- admin endpoints for demos and evals (not part of the "vendor" API) ----

    @app.post("/_admin/faults")
    async def set_faults(request: Request):
        pms.faults.update(await request.json())
        return pms.faults

    @app.post("/_admin/reset")
    def reset():
        pms.reset()
        return {"ok": True}

    @app.get("/_admin/state")
    def state():
        def rows(sql):
            return [dict(r) for r in pms.q(sql)]
        return {
            "faults": pms.faults,
            "requests": pms.request_count,
            "payment_links": rows("SELECT * FROM payment_links"),
            "payment_plans": rows("SELECT * FROM payment_plans"),
            "payments": rows("SELECT * FROM payments"),
            "renewals": rows("SELECT * FROM renewals"),
            "tours": rows("SELECT * FROM tour_slots WHERE booked_by IS NOT NULL"),
            "notes": rows("SELECT * FROM notes ORDER BY id DESC LIMIT 50"),
        }

    return app


app = create_app()
