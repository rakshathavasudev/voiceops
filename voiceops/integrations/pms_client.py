"""Resilient client for the legacy PMS.

Everything the agent knows about a resident comes through here, so this is where
the boring-but-critical integration work lives:
  * OAuth client-credentials with proactive refresh and one retry on 401
  * per-request timeouts, bounded retries with exponential backoff + jitter,
    Retry-After support, and a circuit breaker so a dead PMS fails fast
  * strict validation of the vendor's string-typed payloads into typed models
  * durable writes: intent is persisted to the outbox *before* the call, sent with an
    idempotency key, and retried later if the PMS is down, so nothing is lost and
    nothing is applied twice
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import httpx
from pydantic import BaseModel, field_validator

from voiceops import clock


class PMSError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


class PMSUnavailable(PMSError):
    pass


def _legacy_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(v, "%m/%d/%Y").date()


class Resident(BaseModel):
    ResidentID: str
    FirstName: str
    LastName: str
    UnitID: str
    Phone: str
    Email: str | None = None
    TimeZone: str = "America/Los_Angeles"
    Flags: list[str] = []


class Charge(BaseModel):
    ChargeCode: str
    Description: str
    Amount: Decimal
    DueDate: date

    @field_validator("DueDate", mode="before")
    @classmethod
    def parse_legacy_dates(cls, v: Any) -> date:
        return _legacy_date(v)


class Ledger(BaseModel):
    ResidentID: str
    BalanceDue: Decimal
    DaysPastDue: int
    ActivePaymentPlan: str | None = None
    Charges: list[Charge]

    @field_validator("BalanceDue")
    @classmethod
    def non_negative_cents(cls, v: Decimal) -> Decimal:
        if v.as_tuple().exponent < -2:
            raise ValueError("money with sub-cent precision")
        return v


class Lease(BaseModel):
    LeaseID: str
    StartDate: date
    EndDate: date
    MonthlyRent: Decimal
    RenewalStatus: str

    @field_validator("StartDate", "EndDate", mode="before")
    @classmethod
    def parse_legacy_dates(cls, v: Any) -> date:
        return _legacy_date(v)


class RenewalOffer(BaseModel):
    OfferID: str
    TermMonths: int
    MonthlyRent: Decimal
    ExpiresOn: date

    @field_validator("ExpiresOn", mode="before")
    @classmethod
    def parse_legacy_dates(cls, v: Any) -> date:
        return _legacy_date(v)


class Prospect(BaseModel):
    ProspectID: str
    FirstName: str
    LastName: str
    Phone: str
    Email: str | None = None
    Interest: str | None = None
    Status: str
    LastContact: str | None = None


class TourSlot(BaseModel):
    SlotID: str
    StartsAt: str
    Type: str


@dataclass
class WriteResult:
    status: str  # "committed" | "queued"
    idempotency_key: str
    response: dict | None = None
    replayed: bool = False


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class PMSClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, store, tracer,
                 http: httpx.Client | None = None, timeout: float = 3.0, max_retries: int = 3,
                 backoff_base: float = 0.25, sleep: Callable[[float], None] = time.sleep,
                 breaker_threshold: int = 5, breaker_cooldown: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.store, self.tracer = store, tracer
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=2.0))
        self.timeout = timeout
        self.max_retries, self.backoff_base, self.sleep = max_retries, backoff_base, sleep
        self.breaker_threshold, self.breaker_cooldown = breaker_threshold, breaker_cooldown
        self._token: str | None = None
        self._token_exp = 0.0
        self._consecutive_failures = 0
        self._open_until = 0.0
        self.rng = random.Random()

    # ---- auth ----

    def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_exp - 30:
            return self._token
        with self.tracer.span("pms.auth", kind="http"):
            r = self.http.post(f"{self.base_url}/oauth/token", data={
                "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code != 200:
            raise PMSError("PMS authentication failed", r.status_code, "AUTH")
        body = r.json()
        self._token, self._token_exp = body["access_token"], time.time() + int(body["expires_in"])
        return self._token

    # ---- transport ----

    def breaker_state(self) -> str:
        if time.time() < self._open_until:
            return "open"
        return "half_open" if self._consecutive_failures >= self.breaker_threshold else "closed"

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                 idempotency_key: str | None = None) -> tuple[dict, dict]:
        if self.breaker_state() == "open":
            raise PMSUnavailable("circuit breaker open: PMS is failing, not calling it", code="CIRCUIT_OPEN", retryable=True)
        refreshed = False
        attempt = 0
        last_error = "unknown"
        while attempt <= self.max_retries:
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            with self.tracer.span(f"pms {method} {path.split('?')[0]}", kind="http", attempt=attempt + 1) as sp:
                try:
                    r = self.http.request(method, f"{self.base_url}{path}", params=params, json=body,
                                          headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    sp.error(f"{type(exc).__name__}")
                    last_error, retry_after = type(exc).__name__, None
                else:
                    sp.set(status=r.status_code, replay=r.headers.get("Idempotent-Replay") == "true")
                    if r.status_code == 401 and not refreshed:
                        refreshed = True
                        self._get_token(force=True)
                        continue
                    if r.status_code < 400:
                        self._consecutive_failures = 0
                        return r.json(), dict(r.headers)
                    detail = _detail(r)
                    if r.status_code not in RETRYABLE_STATUS:
                        sp.error(detail.get("ErrorCode", str(r.status_code)))
                        raise PMSError(detail.get("Message", "PMS rejected the request"), r.status_code, detail.get("ErrorCode"))
                    sp.error(detail.get("ErrorCode", str(r.status_code)))
                    last_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
            attempt += 1
            if attempt <= self.max_retries:
                delay = float(retry_after) if retry_after else self.backoff_base * (2 ** (attempt - 1))
                self.sleep(delay * (0.5 + self.rng.random()))  # jitter avoids synchronized retry storms
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker_threshold:
            self._open_until = time.time() + self.breaker_cooldown
        raise PMSUnavailable(f"PMS unavailable after {self.max_retries + 1} attempts ({last_error})", code="UNAVAILABLE", retryable=True)

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})[0]

    # ---- typed reads ----

    def find_residents(self, phone: str | None = None, unit: str | None = None, email: str | None = None) -> list[Resident]:
        return [Resident(**r) for r in self.get("/api/v1/residents", phone=phone, unit=unit, email=email)["Residents"]]

    def get_resident(self, rid: str) -> Resident:
        return Resident(**self.get(f"/api/v1/residents/{rid}"))

    def ledger(self, rid: str) -> Ledger:
        return Ledger(**self.get(f"/api/v1/residents/{rid}/ledger"))

    def lease(self, rid: str) -> Lease:
        return Lease(**self.get(f"/api/v1/residents/{rid}/lease"))

    def renewal_offers(self, rid: str) -> list[RenewalOffer]:
        return [RenewalOffer(**o) for o in self.get(f"/api/v1/residents/{rid}/renewal-offers")["Offers"]]

    def find_prospects(self, phone: str | None = None, email: str | None = None) -> list[Prospect]:
        return [Prospect(**p) for p in self.get("/api/v1/prospects", phone=phone, email=email)["Prospects"]]

    def get_prospect(self, pid: str) -> Prospect:
        return Prospect(**self.get(f"/api/v1/prospects/{pid}"))

    def tour_slots(self) -> list[TourSlot]:
        return [TourSlot(**s) for s in self.get("/api/v1/tour-slots")["Slots"]]

    # ---- durable writes ----

    def write(self, op: str, path: str, payload: dict, idempotency_key: str, conversation_id: str | None) -> WriteResult:
        now = clock.now().isoformat()
        existing = self.store.q("SELECT * FROM pending_writes WHERE idempotency_key=?", idempotency_key)
        if existing and existing[0]["status"] == "done":
            return WriteResult("committed", idempotency_key, json.loads(existing[0]["response"]), replayed=True)
        if not existing:
            # Persist intent first: if we crash mid-call the drain worker finishes the job.
            self.store.x("INSERT INTO pending_writes(idempotency_key, conversation_id, op, path, payload, status, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", idempotency_key, conversation_id, op, path, json.dumps(payload), "pending", now, now)
        return self._attempt_write(idempotency_key, path, payload)

    def _attempt_write(self, key: str, path: str, payload: dict) -> WriteResult:
        self.store.x("UPDATE pending_writes SET attempts=attempts+1, updated_at=? WHERE idempotency_key=?", clock.now().isoformat(), key)
        try:
            body, headers = self._request("POST", path, body=payload, idempotency_key=key)
        except PMSUnavailable as exc:
            self.store.x("UPDATE pending_writes SET last_error=? WHERE idempotency_key=?", str(exc), key)
            return WriteResult("queued", key)
        except PMSError as exc:
            self.store.x("UPDATE pending_writes SET status='failed', last_error=? WHERE idempotency_key=?", str(exc), key)
            raise
        self.store.x("UPDATE pending_writes SET status='done', response=?, updated_at=? WHERE idempotency_key=?",
                     json.dumps(body), clock.now().isoformat(), key)
        return WriteResult("committed", key, body, replayed=headers.get("idempotent-replay") == "true")

    def drain_outbox(self) -> list[dict]:
        """Retry queued writes. Safe to run repeatedly or concurrently with live traffic."""
        results = []
        for row in self.store.q("SELECT * FROM pending_writes WHERE status='pending' ORDER BY id"):
            try:
                res = self._attempt_write(row["idempotency_key"], row["path"], json.loads(row["payload"]))
                results.append({"key": row["idempotency_key"], "op": row["op"], "status": res.status,
                                "conversation_id": row["conversation_id"], "response": res.response})
            except PMSError as exc:
                results.append({"key": row["idempotency_key"], "op": row["op"], "status": "failed", "error": str(exc),
                                "conversation_id": row["conversation_id"]})
        return results


def _detail(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    d = body.get("detail", body)
    return d if isinstance(d, dict) else {"Message": str(d)}
