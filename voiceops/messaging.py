"""Outbound messages (SMS / email). Everything lands in the outbox table; SMS also goes
through Twilio when credentials are configured. Consent is re-checked here as a last
line of defense, regardless of what the caller already checked."""
from __future__ import annotations

import httpx

from voiceops import clock
from voiceops.agent.guardrails import redact


class ConsentError(Exception):
    pass


class Messenger:
    def __init__(self, store, settings, tracer) -> None:
        self.store, self.settings, self.tracer = store, settings, tracer

    def send(self, conversation_id: str | None, subject_id: str, channel: str, to: str, body: str,
             transactional: bool = False) -> dict:
        consent = self.store.consent(subject_id, channel)
        # Transactional replies the user just asked for (e.g. a requested payment link) are allowed
        # unless the user revoked consent; marketing/outbound needs an explicit grant.
        if consent == "revoked" or (consent != "granted" and not transactional):
            raise ConsentError(f"no {channel} consent for {subject_id} (status: {consent})")
        body, _ = redact(body)
        provider_id = None
        status = "sent_mock"
        with self.tracer.span(f"send {channel}", kind="messaging", to=to[-4:]) as sp:
            s = self.settings
            if channel == "sms" and s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number:
                r = httpx.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
                    data={"To": to, "From": s.twilio_from_number, "Body": body},
                    auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=5.0)
                status = "sent" if r.status_code < 300 else f"error_{r.status_code}"
                provider_id = r.json().get("sid") if r.status_code < 300 else None
            sp.set(status=status)
        self.store.x("INSERT INTO outbox(conversation_id, channel, to_addr, body, status, provider_id, created_at) VALUES (?,?,?,?,?,?,?)",
                     conversation_id, channel, to, body, status, provider_id, clock.now().isoformat())
        return {"channel": channel, "status": status}
