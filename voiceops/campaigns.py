"""Compliant outbound workflows: collections, renewals, lead follow-up and abandoned applications.

Every candidate goes through the same compliance gate before anything is sent, and every
decision (sent or skipped, and why) is recorded so operations can audit the run.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from voiceops import clock
from voiceops.integrations import twilio
from voiceops.messaging import ConsentError

CAMPAIGNS = ("collections", "renewal", "lead_followup", "application_followup")


def _targets(rt, campaign: str) -> list[dict]:
    pms, org = rt.s.pms, rt.s.org
    out = []
    today = clock.now().date()
    if campaign in ("collections", "renewal"):
        for r in pms.find_residents():
            base = {"subject_type": "resident", "subject_id": r.ResidentID, "name": f"{r.FirstName} {r.LastName}",
                    "phone": r.Phone, "email": r.Email, "tz": r.TimeZone}
            if campaign == "collections":
                ledger = pms.ledger(r.ResidentID)
                if ledger.BalanceDue <= 0 or ledger.DaysPastDue < org["outbound"]["collections_min_days_past_due"]:
                    continue
                skip = "open billing dispute" if "DISPUTE" in r.Flags else "active payment plan" if ledger.ActivePaymentPlan else None
                out.append({**base, "skip": skip, "detail": f"${ledger.BalanceDue}, {ledger.DaysPastDue} days past due"})
            else:
                lease = pms.lease(r.ResidentID)
                if lease.RenewalStatus != "OFFERED" or (lease.EndDate - today).days > org["outbound"]["renewal_window_days"]:
                    continue
                out.append({**base, "skip": None, "detail": f"lease ends {lease.EndDate}"})
    else:
        want = "NEW" if campaign == "lead_followup" else "APPLICATION_STARTED"
        for p in pms.find_prospects():
            if p.Status != want:
                continue
            last = datetime.fromisoformat(p.LastContact) if p.LastContact else None
            if campaign == "lead_followup" and last and (clock.now().replace(tzinfo=None) - last) < timedelta(hours=org["outbound"]["lead_followup_after_hours"]):
                continue
            out.append({"subject_type": "prospect", "subject_id": p.ProspectID, "name": f"{p.FirstName} {p.LastName}",
                        "phone": p.Phone, "email": p.Email, "tz": org["timezone"], "skip": None, "detail": p.Interest})
    return out


def compliance_check(rt, target: dict, channel: str) -> str | None:
    """Return a skip reason, or None if contact is allowed right now."""
    store, org = rt.s.store, rt.s.org
    if target.get("skip"):
        return target["skip"]
    consent = store.consent(target["subject_id"], channel)
    if consent != "granted":
        return f"no {channel} consent ({consent})"
    local = clock.now().astimezone(ZoneInfo(target["tz"]))
    start = time.fromisoformat(org["outbound"]["quiet_hours_end"])
    end = time.fromisoformat(org["outbound"]["quiet_hours_start"])
    if not (start <= local.time().replace(tzinfo=None) < end):
        return f"quiet hours ({local:%H:%M} {local.tzname()} local)"
    since = (clock.now() - timedelta(days=7)).isoformat()
    sent = store.q("SELECT COUNT(*) n FROM contact_attempts WHERE subject_id=? AND decision='sent' AND created_at>=?",
                   target["subject_id"], since)[0]["n"]
    if sent >= org["outbound"]["max_attempts_per_7_days"]:
        return f"frequency cap ({sent} contacts in 7 days)"
    open_cases = store.q("SELECT 1 FROM conversations WHERE subject_id=? AND status='escalated'", target["subject_id"])
    if open_cases:
        return "case is with a human agent"
    return None


def run_campaign(rt, campaign: str, channel: str = "sms") -> list[dict]:
    if campaign not in CAMPAIGNS:
        raise ValueError(f"unknown campaign {campaign}")
    s = rt.s
    decisions = []
    with s.tracer.span(f"campaign {campaign}", kind="campaign", new_trace=True, channel=channel):
        for target in _targets(rt, campaign):
            reason = compliance_check(rt, target, channel)
            conv_id = None
            if reason is None:
                conv = s.store.open_conversation(s.org_id, target["subject_type"], target["subject_id"], target["name"], campaign)
                conv_id = conv["id"]
                opening = rt.opening_message(conv, channel)
                try:
                    if channel == "voice":
                        rt.start_outbound(conv, channel)
                        base = s.settings.public_base_url if s.settings else None
                        sid = twilio.place_call(s.settings, target["phone"], f"{base}/twilio/voice/outbound?conv={conv_id}") if base else None
                        s.store.add_message(conv_id, None, channel, "system", f"outbound call {'placed ' + sid if sid else 'queued in mock dialer'}")
                    else:
                        s.messenger.send(conv_id, target["subject_id"], channel, target["phone"] if channel == "sms" else target["email"], opening)
                        rt.start_outbound(conv, channel)
                except ConsentError as exc:
                    reason = str(exc)
            decision = "skipped" if reason else "sent"
            s.store.x("INSERT INTO contact_attempts(subject_id, campaign, channel, decision, reason, conversation_id, created_at) VALUES (?,?,?,?,?,?,?)",
                      target["subject_id"], campaign, channel, decision, reason, conv_id, clock.now().isoformat())
            s.store.audit(s.org_id, f"campaign:{campaign}", f"outbound.{decision}", target["subject_id"],
                          {"channel": channel, "reason": reason, "detail": target["detail"]}, decision)
            decisions.append({"subject_id": target["subject_id"], "name": target["name"], "decision": decision,
                              "reason": reason, "detail": target["detail"], "conversation_id": conv_id})
    return decisions

