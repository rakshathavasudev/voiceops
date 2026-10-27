"""Twilio helpers: webhook signature validation, TwiML builders, outbound call placement."""
from __future__ import annotations

import base64
import hashlib
import hmac
from xml.sax.saxutils import escape, quoteattr

import httpx

VOICE = "Polly.Joanna-Neural"


def valid_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """https://www.twilio.com/docs/usage/security#validating-requests"""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def say_and_gather(text: str, action: str) -> str:
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
            f"<Gather input=\"speech\" action={quoteattr(action)} speechTimeout=\"auto\" language=\"en-US\">"
            f"<Say voice=\"{VOICE}\">{escape(text)}</Say></Gather>"
            f"<Redirect>{escape(action + '&silence=1')}</Redirect></Response>")


def say_and_dial(text: str, number: str) -> str:
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say voice=\"{VOICE}\">{escape(text)}</Say>"
            f"<Dial>{escape(number)}</Dial></Response>")


def say_and_hangup(text: str) -> str:
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say voice=\"{VOICE}\">{escape(text)}</Say><Hangup/></Response>"


def message(text: str) -> str:
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{escape(text)}</Message></Response>"


def place_call(settings, to: str, answer_url: str) -> str | None:
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
        return None
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json",
                   data={"To": to, "From": settings.twilio_from_number, "Url": answer_url},
                   auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=5.0)
    r.raise_for_status()
    return r.json().get("sid")
