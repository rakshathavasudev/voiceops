"""Tools the model can call, and the policy layer each call passes through.

Design choices worth calling out:
  * Scope by construction: no tool takes a resident ID. The conversation is bound to one
    verified subject, so the model cannot read or write anyone else's record even if a
    prompt injection asks it to.
  * Two-phase writes: prepare_* validates against policy and returns a read-back; only
    commit_confirmed_action writes, and only after the user affirms in a later turn.
  * Idempotency keys derive from (conversation, action, params), so a retried commit,
    a model that calls commit twice, or a replayed webhook all collapse to one write.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from voiceops import clock
from voiceops.agent import knowledge
from voiceops.agent.guardrails import (PolicyViolation, is_affirmative, validate_payment_amount,
                                       validate_payment_plan)
from voiceops.integrations.pms_client import PMSError, PMSUnavailable
from voiceops.messaging import ConsentError


class ToolBlocked(Exception):
    """A guardrail refused the call. The message goes back to the model as an error result."""

    def __init__(self, guardrail: str, message: str, **extra):
        super().__init__(message)
        self.guardrail, self.extra = guardrail, extra


@dataclass
class ToolContext:
    services: Any  # voiceops.agent.runtime.Services
    conv: dict
    turn_id: str
    turn_index: int
    channel: str
    user_text: str
    blocked: list[dict] = field(default_factory=list)

    @property
    def org(self) -> dict:
        return self.services.org

    def refresh(self) -> None:
        self.conv = self.services.store.get_conversation(self.conv["id"])

    def update(self, **fields) -> None:
        self.services.store.update_conversation(self.conv["id"], **fields)
        self.refresh()

    def verified(self) -> bool:
        vu = self.conv.get("verified_until")
        return bool(vu) and datetime.fromisoformat(vu) > clock.now()

    def require_verified_resident(self) -> str:
        if self.conv["subject_type"] != "resident" or not self.conv["subject_id"]:
            raise ToolBlocked("scope", "This conversation is not linked to a resident account.")
        if not self.verified():
            raise ToolBlocked("verification", "Identity not verified. Ask for last name and unit number and call verify_identity first.")
        return self.conv["subject_id"]

    def audit(self, action: str, details: dict, outcome: str, key: str | None = None) -> None:
        from voiceops.tracing import current_trace_id
        self.services.store.audit(self.services.org_id, f"agent:{self.conv['id']}", action,
                                  self.conv.get("subject_id") or "unknown", details, outcome, key, current_trace_id())


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props),
            "additionalProperties": False}


TOOL_SPECS: list[dict] = [
    {"name": "verify_identity",
     "description": "Verify the resident before sharing or changing anything on their account. Ask the caller for their last name and unit number, then call this. Never reveal which factor was wrong.",
     "input_schema": _obj({"last_name": {"type": "string"}, "unit_number": {"type": "string"}})},
    {"name": "get_account_summary",
     "description": "Verified residents only. Returns balance due, days past due, charges, active payment plan, lease end date and the org's payment plan limits.",
     "input_schema": _obj({})},
    {"name": "get_renewal_offers",
     "description": "Verified residents only. Returns the lease renewal offers available to this resident.",
     "input_schema": _obj({})},
    {"name": "search_policy",
     "description": "Search the property's policy handbook (late fees, ways to pay, payment plans, renewals, office hours, pets, parking, emergencies, tours, applications). Answer policy questions only from what this returns.",
     "input_schema": _obj({"query": {"type": "string"}})},
    {"name": "prepare_payment_link",
     "description": "Step 1 of 2 for a payment. Validates the amount and returns a read-back. Read it back word for word and ask the resident to confirm. A secure link is sent by SMS or email; never collect card numbers yourself.",
     "input_schema": _obj({"amount": {"type": "string", "description": "Dollar amount, e.g. \"1450.00\""},
                           "send_via": {"type": "string", "enum": ["sms", "email"]}})},
    {"name": "prepare_payment_plan",
     "description": "Step 1 of 2 for a payment plan. Installments must sum to the full balance and follow the plan limits from get_account_summary. Returns a read-back to confirm with the resident.",
     "input_schema": _obj({"installments": {"type": "array", "items": _obj({
         "amount": {"type": "string", "description": "Dollar amount, e.g. \"483.34\""},
         "due_date": {"type": "string", "description": "YYYY-MM-DD"}})}})},
    {"name": "prepare_lease_renewal",
     "description": "Step 1 of 2 to accept a renewal offer from get_renewal_offers. Returns a read-back to confirm with the resident.",
     "input_schema": _obj({"offer_id": {"type": "string"}})},
    {"name": "get_tour_slots",
     "description": "Prospects: list open tour times.",
     "input_schema": _obj({})},
    {"name": "prepare_tour_booking",
     "description": "Step 1 of 2 to book a tour for a prospect. Returns a read-back to confirm.",
     "input_schema": _obj({"slot_id": {"type": "string"}})},
    {"name": "commit_confirmed_action",
     "description": "Step 2 of 2. Call only after the user explicitly said yes to the read-back in their latest message. The system independently checks the user's words; a missing or ambiguous confirmation is rejected.",
     "input_schema": _obj({"confirmation_id": {"type": "string"}})},
    {"name": "send_message",
     "description": "Send a short follow-up (summary, reminder) to the subject's contact on file. You cannot choose the destination.",
     "input_schema": _obj({"channel": {"type": "string", "enum": ["sms", "email"]}, "text": {"type": "string"}})},
    {"name": "record_opt_out",
     "description": "Record that the person does not want to be contacted on a channel.",
     "input_schema": _obj({"channel": {"type": "string", "enum": ["sms", "voice", "email"]}})},
    {"name": "escalate_to_human",
     "description": "Hand the conversation to the leasing office. Use for disputes, hardship, complaints, fee waivers, anything outside policy, or when you are unsure.",
     "input_schema": _obj({"category": {"type": "string", "enum": ["dispute", "hardship", "policy_exception", "complaint", "human_request", "technical", "other"]},
                           "reason": {"type": "string"}, "summary": {"type": "string", "description": "Context for the staff member taking over"}})},
]

for _spec in TOOL_SPECS:
    _spec["strict"] = True



def idempotency_key(org_id: str, conv_id: str, action: str, params: dict) -> str:
    raw = json.dumps([org_id, conv_id, action, params], sort_keys=True)
    return "vo-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------- handlers ----------------

def verify_identity(ctx: ToolContext, last_name: str, unit_number: str) -> dict:
    s, cfg = ctx.services, ctx.org["verification"]
    if ctx.conv["subject_type"] == "prospect":
        return {"verified": True, "note": "Prospects do not need verification."}
    if ctx.verified():
        return {"verified": True}
    if ctx.conv["verify_failures"] >= cfg["max_attempts"]:
        raise ToolBlocked("verification", "Verification is locked for this conversation. Escalate to a human.")
    unit = unit_number.strip().upper().removeprefix("UNIT").strip().lstrip("#")
    candidate = None
    if ctx.conv["subject_id"]:
        candidate = s.pms.get_resident(ctx.conv["subject_id"])
    else:
        matches = s.pms.find_residents(unit=unit)
        candidate = matches[0] if matches else None
    ok = bool(candidate) and candidate.LastName.strip().lower() == last_name.strip().lower() and candidate.UnitID.upper() == unit
    if ok:
        until = clock.now() + timedelta(minutes=cfg["session_minutes"])
        ctx.update(verified_until=until.isoformat(), subject_type="resident", subject_id=candidate.ResidentID,
                   display_name=f"{candidate.FirstName} {candidate.LastName}")
        ctx.audit("identity.verified", {"factors": cfg["factors"], "channel": ctx.channel}, "success")
        return {"verified": True, "first_name": candidate.FirstName}
    failures = ctx.conv["verify_failures"] + 1
    ctx.update(verify_failures=failures)
    ctx.audit("identity.verification_failed", {"attempt": failures, "channel": ctx.channel}, "failure")
    left = cfg["max_attempts"] - failures
    if left <= 0:
        _escalate(ctx, "verification_failed", "Caller failed identity verification", "Verification attempts exhausted.")
        return {"verified": False, "locked": True, "instruction": "Do not share any account details. Tell the caller a team member will follow up."}
    return {"verified": False, "attempts_left": left, "instruction": "Do not say which detail was wrong. Ask them to repeat their last name and unit number."}


def get_account_summary(ctx: ToolContext) -> dict:
    rid = ctx.require_verified_resident()
    ledger, lease = ctx.services.pms.ledger(rid), ctx.services.pms.lease(rid)
    rules = ctx.org["payment_plans"]
    today = clock.now().date()
    return {
        "balance_due": str(ledger.BalanceDue), "days_past_due": ledger.DaysPastDue,
        "charges": [{"description": c.Description, "amount": str(c.Amount), "due_date": c.DueDate.isoformat()} for c in ledger.Charges[-4:]],
        "active_payment_plan": ledger.ActivePaymentPlan,
        "lease_end_date": lease.EndDate.isoformat(), "monthly_rent": str(lease.MonthlyRent), "renewal_status": lease.RenewalStatus,
        "today": today.isoformat(),
        "payment_plan_limits": {"max_installments": rules["max_installments"],
                                "final_installment_due_by": (today + timedelta(days=rules["max_days_to_final_installment"])).isoformat(),
                                "min_first_installment": str((ledger.BalanceDue * Decimal(str(rules["min_first_installment_pct"]))).quantize(Decimal("0.01"))),
                                "fee_waivers_allowed": rules["allow_fee_waivers"]},
    }


def get_renewal_offers(ctx: ToolContext) -> dict:
    rid = ctx.require_verified_resident()
    offers = ctx.services.pms.renewal_offers(rid)
    return {"offers": [{"offer_id": o.OfferID, "term_months": o.TermMonths, "monthly_rent": str(o.MonthlyRent),
                        "expires_on": o.ExpiresOn.isoformat()} for o in offers]}


def search_policy(ctx: ToolContext, query: str) -> dict:
    return {"results": knowledge.search(ctx.services.org_id, query)}


def _set_pending(ctx: ToolContext, action: str, params: dict, readback: str) -> dict:
    cid = "CF-" + uuid.uuid4().hex[:6].upper()
    pending = {"id": cid, "action": action, "params": params, "readback": readback,
               "turn_index": ctx.turn_index, "created_at": clock.now().isoformat()}
    ctx.update(pending_confirmation=json.dumps(pending))
    ctx.services.store.add_event(ctx.conv["id"], ctx.turn_id, "confirmation_requested", {"id": cid, "action": action, "readback": readback})
    return {"confirmation_id": cid, "readback": readback,
            "instruction": "Read this back and ask for an explicit yes. Do not call commit_confirmed_action until the user answers."}


def prepare_payment_link(ctx: ToolContext, amount: str, send_via: str) -> dict:
    rid = ctx.require_verified_resident()
    ledger = ctx.services.pms.ledger(rid)
    try:
        amt = validate_payment_amount(amount, ledger.BalanceDue, ctx.org)
    except PolicyViolation as v:
        raise ToolBlocked("policy", v.reason, allowed=v.allowed) from v
    if ctx.services.store.consent(rid, send_via) == "revoked":
        raise ToolBlocked("consent", f"The resident opted out of {send_via}. Offer the other channel.")
    return _set_pending(ctx, "payment_link", {"amount": str(amt), "send_via": send_via},
                        f"I'll text you a secure payment link for ${amt}" if send_via == "sms"
                        else f"I'll email you a secure payment link for ${amt}")


def prepare_payment_plan(ctx: ToolContext, installments: list[dict]) -> dict:
    rid = ctx.require_verified_resident()
    ledger = ctx.services.pms.ledger(rid)
    try:
        clean = validate_payment_plan(installments, ledger.BalanceDue, clock.now().date(), ctx.org, bool(ledger.ActivePaymentPlan))
    except PolicyViolation as v:
        raise ToolBlocked("policy", v.reason, allowed=v.allowed) from v
    parts = ", ".join(f"${i['amount']} on {date.fromisoformat(i['due_date']):%B %-d}" for i in clean)
    return _set_pending(ctx, "payment_plan", {"installments": clean},
                        f"A payment plan for your ${ledger.BalanceDue} balance in {len(clean)} payments: {parts}")


def prepare_lease_renewal(ctx: ToolContext, offer_id: str) -> dict:
    rid = ctx.require_verified_resident()
    offer = next((o for o in ctx.services.pms.renewal_offers(rid) if o.OfferID == offer_id), None)
    if not offer:
        raise ToolBlocked("scope", f"Offer {offer_id} is not available to this resident.")
    return _set_pending(ctx, "lease_renewal", {"offer_id": offer_id},
                        f"Renewing your lease for {offer.TermMonths} months at ${offer.MonthlyRent} per month. "
                        "You'll get an addendum to e-sign within 7 days")


def get_tour_slots(ctx: ToolContext) -> dict:
    if ctx.conv["subject_type"] != "prospect":
        raise ToolBlocked("scope", "Tours are for prospective residents.")
    return {"slots": [{"slot_id": t.SlotID, "starts_at": t.StartsAt, "type": t.Type} for t in ctx.services.pms.tour_slots()[:6]]}


def prepare_tour_booking(ctx: ToolContext, slot_id: str) -> dict:
    if ctx.conv["subject_type"] != "prospect":
        raise ToolBlocked("scope", "Tours are for prospective residents.")
    slot = next((t for t in ctx.services.pms.tour_slots() if t.SlotID == slot_id), None)
    if not slot:
        raise ToolBlocked("scope", f"Slot {slot_id} is not open.")
    when = datetime.fromisoformat(slot.StartsAt)
    kind = "an agent-led" if slot.Type == "AGENT_LED" else "a self-guided"
    return _set_pending(ctx, "tour_booking", {"slot_id": slot_id},
                        f"Booking {kind} tour on {when:%A, %B %-d at %-I:%M %p}")


def commit_confirmed_action(ctx: ToolContext, confirmation_id: str) -> dict:
    s = ctx.services
    pending = json.loads(ctx.conv["pending_confirmation"]) if ctx.conv["pending_confirmation"] else None
    # Every check here is deterministic and independent of what the model believes.
    if not pending or pending["id"] != confirmation_id:
        raise ToolBlocked("confirmation", "No matching action is awaiting confirmation.")
    if pending["turn_index"] >= ctx.turn_index:
        raise ToolBlocked("confirmation", "You must read the action back and wait for the user's reply before committing.")
    if not is_affirmative(ctx.user_text):
        raise ToolBlocked("confirmation", "The user's latest message is not a clear yes. Ask them to confirm explicitly.")
    if datetime.fromisoformat(pending["created_at"]) < clock.now() - timedelta(minutes=10):
        ctx.update(pending_confirmation=None)
        raise ToolBlocked("confirmation", "The confirmation expired. Prepare the action again.")
    action, params = pending["action"], pending["params"]
    key = idempotency_key(s.org_id, ctx.conv["id"], action, params)
    sid = ctx.conv["subject_id"]
    if action == "payment_link":
        result = s.pms.write(action, "/api/v1/payment-links", {"ResidentID": sid, "Amount": params["amount"]}, key, ctx.conv["id"])
    elif action == "payment_plan":
        result = s.pms.write(action, "/api/v1/payment-plans", {"ResidentID": sid, "Installments": [
            {"Amount": i["amount"], "DueDate": date.fromisoformat(i["due_date"]).strftime("%m/%d/%Y")} for i in params["installments"]]},
            key, ctx.conv["id"])
    elif action == "lease_renewal":
        result = s.pms.write(action, "/api/v1/lease-renewals", {"ResidentID": sid, "OfferID": params["offer_id"]}, key, ctx.conv["id"])
    elif action == "tour_booking":
        result = s.pms.write(action, "/api/v1/tours", {"ProspectID": sid, "SlotID": params["slot_id"]}, key, ctx.conv["id"])
    else:
        raise ToolBlocked("scope", f"Unknown action {action}")
    ctx.update(pending_confirmation=None)
    ctx.audit(f"{action}.{result.status}", {"params": params, "confirmation_id": confirmation_id,
                                             "user_confirmation": ctx.user_text[:200], "response": result.response,
                                             "replayed": result.replayed}, result.status, key)
    s.store.add_event(ctx.conv["id"], ctx.turn_id, f"{action}_{result.status}", {"params": params, "response": result.response})
    out: dict = {"status": result.status, "action": action}
    if result.status == "queued":
        out["instruction"] = ("The system of record is temporarily unavailable. The request is saved and will be applied "
                              "automatically. Tell the user it is being processed and they will get a confirmation; do not say it is done.")
        return out
    out["record"] = result.response
    if action == "payment_link":
        out["delivery"] = _deliver_link(ctx, params["send_via"], result.response["Url"], params["amount"])
    return out


def _contact(ctx: ToolContext, channel: str) -> str:
    sid, pms = ctx.conv["subject_id"], ctx.services.pms
    if ctx.conv["subject_type"] == "resident":
        r = pms.get_resident(sid)
        return r.Phone if channel in ("sms", "voice") else r.Email
    p = pms.get_prospect(sid)
    return p.Phone if channel in ("sms", "voice") else p.Email


def _deliver_link(ctx: ToolContext, channel: str, url: str, amount: str) -> dict:
    name = ctx.services.org["display_name"]
    body = (f"{name}: here is your secure payment link for ${amount}: {url} . It expires in "
            f"{ctx.org['payments']['payment_link_ttl_hours']} hours. Reply STOP to opt out.")
    try:
        return ctx.services.messenger.send(ctx.conv["id"], ctx.conv["subject_id"], channel, _contact(ctx, channel), body, transactional=True)
    except ConsentError as exc:
        return {"status": "not_sent", "reason": str(exc)}


def send_message(ctx: ToolContext, channel: str, text: str) -> dict:
    if ctx.conv["subject_type"] == "resident":
        ctx.require_verified_resident()
    try:
        res = ctx.services.messenger.send(ctx.conv["id"], ctx.conv["subject_id"], channel, _contact(ctx, channel), text[:600], transactional=True)
    except ConsentError as exc:
        raise ToolBlocked("consent", str(exc)) from exc
    ctx.audit("message.sent", {"channel": channel}, "success")
    return res


def record_opt_out(ctx: ToolContext, channel: str) -> dict:
    if ctx.conv["subject_id"]:
        ctx.services.store.set_consent(ctx.conv["subject_id"], channel, "revoked", f"agent:{ctx.channel}")
        ctx.audit("consent.revoked", {"channel": channel, "source": ctx.channel}, "success")
    return {"recorded": True, "channel": channel}


def _escalate(ctx: ToolContext, category: str, reason: str, summary: str) -> dict:
    s = ctx.services
    eid = s.store.x("INSERT INTO escalations(conversation_id, category, reason, summary, status, created_at) VALUES (?,?,?,?,?,?)",
                    ctx.conv["id"], category, reason, summary, "open", clock.now().isoformat())
    ctx.update(status="escalated", pending_confirmation=None)
    ctx.audit("conversation.escalated", {"category": category, "reason": reason}, "success")
    s.store.add_event(ctx.conv["id"], ctx.turn_id, "escalated", {"category": category, "reason": reason})
    return {"escalation_id": eid, "queue": s.org["escalation"]["queue"],
            "handoff": "warm transfer to the leasing office" if ctx.channel == "voice" else "a team member will reply here",
            "office_phone": s.org["office_phone"]}


def escalate_to_human(ctx: ToolContext, category: str, reason: str, summary: str) -> dict:
    return _escalate(ctx, category, reason, summary)


HANDLERS = {
    "verify_identity": verify_identity, "get_account_summary": get_account_summary,
    "get_renewal_offers": get_renewal_offers, "search_policy": search_policy,
    "prepare_payment_link": prepare_payment_link, "prepare_payment_plan": prepare_payment_plan,
    "prepare_lease_renewal": prepare_lease_renewal, "get_tour_slots": get_tour_slots,
    "prepare_tour_booking": prepare_tour_booking, "commit_confirmed_action": commit_confirmed_action,
    "send_message": send_message, "record_opt_out": record_opt_out, "escalate_to_human": escalate_to_human,
}


def execute(ctx: ToolContext, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool call. Returns (result, is_error). Never raises into the model loop."""
    handler = HANDLERS.get(name)
    if not handler:
        return {"error": f"unknown tool {name}"}, True
    try:
        return handler(ctx, **args), False
    except ToolBlocked as b:
        ctx.blocked.append({"tool": name, "guardrail": b.guardrail, "reason": str(b)})
        ctx.services.store.add_event(ctx.conv["id"], ctx.turn_id, "guardrail_blocked", {"tool": name, "guardrail": b.guardrail, "reason": str(b)})
        return {"error": str(b), "blocked_by": b.guardrail, **b.extra}, True
    except PMSUnavailable as exc:
        return {"error": "The property system is temporarily unavailable.", "detail": str(exc),
                "instruction": "Apologize, offer to follow up by text, or escalate if the user needs help now."}, True
    except PMSError as exc:
        return {"error": f"The property system rejected the request: {exc}", "code": exc.code}, True
    except TypeError as exc:
        return {"error": f"invalid arguments for {name}: {exc}"}, True
