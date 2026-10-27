"""Deterministic guardrails that sit outside the model.

The model is good at conversation and bad at being a security boundary, so the
rules that protect money and tenant records are plain code:
  * redaction of card numbers / SSNs before text reaches the model, logs or storage
  * hard triggers (emergencies, opt-outs, legal threats, disputes, "get me a human")
    that bypass the model entirely
  * the confirmation check: a write only commits if the *user's own words* in a later
    turn affirm the exact read-back the agent gave
  * policy validation for payment plans and payment amounts, from org config
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# ---------------- redaction ----------------

_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CVV = re.compile(r"\b(?:cvv|cvc|security code)\D{0,12}\d{3,4}\b", re.I)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_found). Runs before anything is stored or sent to the model."""
    found: list[str] = []

    def pan(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card_number")
            return "[REDACTED CARD]"
        return m.group(0)

    text = _PAN.sub(pan, text)
    if _SSN.search(text):
        found.append("ssn")
        text = _SSN.sub("[REDACTED SSN]", text)
    if _CVV.search(text):
        found.append("card_security_code")
        text = _CVV.sub("[REDACTED CVV]", text)
    return text, found


# ---------------- hard triggers ----------------

@dataclass
class Trigger:
    category: str
    reason: str


_SMS_STOP = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout", "opt out"}
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("emergency", re.compile(r"\b(flood(ing|ed)?|water (is )?(leaking|pouring|coming)|fire|smoke|gas leak|smell(s)? (like )?gas|"
                             r"carbon monoxide|no heat|locked out|sparking|burst pipe)\b", re.I)),
    ("opt_out", re.compile(r"\b(stop (calling|texting|contacting) me|do not (call|text|contact) me|don'?t (call|text|contact) me( again)?|"
                           r"remove me from (your|the) (list|calls))\b", re.I)),
    ("legal", re.compile(r"\b(attorney|lawyer|sue|suing|lawsuit|legal action|small claims|discriminat\w*|fair housing)\b", re.I)),
    ("dispute", re.compile(r"\b(already paid|i paid (that|this|it)|dispute|not my charge|wrong (amount|balance)|"
                           r"incorrect (charge|balance)|overcharg\w*|that'?s not right|charged twice|double charged)\b", re.I)),
    ("human_request", re.compile(r"\b((speak|talk) (to|with) (a |an |the )?(human|person|real person|someone|agent|representative|manager|leasing office)|"
                                 r"representative|transfer me|real person|human please|operator)\b", re.I)),
]


def hard_trigger(text: str, channel: str) -> Trigger | None:
    if channel == "sms" and text.strip().lower().strip(".!") in _SMS_STOP:
        return Trigger("opt_out", "carrier opt-out keyword")
    if channel == "sms" and text.strip().lower().strip(".!") in ("start", "unstop"):
        return Trigger("opt_in", "carrier opt-in keyword")
    for category, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            return Trigger(category, f"matched '{m.group(0)}'")
    return None


# ---------------- confirmation ----------------

_YES = re.compile(r"\b(yes|yeah|yep|yup|correct|confirm(ed)?|go ahead|sounds good|do it|that works|please do|"
                  r"that'?s right|absolutely|sure|ok(ay)?|perfect|let'?s do (it|that))\b", re.I)
_NO = re.compile(r"\b(no|nope|don'?t|do not|wait|hold on|stop|cancel|not yet|actually|never ?mind|change|instead|wrong)\b", re.I)


def is_affirmative(text: str) -> bool:
    return bool(_YES.search(text)) and not _NO.search(text)


def is_negative(text: str) -> bool:
    return bool(_NO.search(text)) and not _YES.search(text) or bool(re.match(r"\s*(no|nope|cancel)\b", text, re.I))


# ---------------- policy validation ----------------

class PolicyViolation(Exception):
    def __init__(self, reason: str, allowed: dict | None = None):
        super().__init__(reason)
        self.reason, self.allowed = reason, allowed or {}


def money(v) -> Decimal:
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise PolicyViolation(f"'{v}' is not a valid amount") from None
    if d <= 0:
        raise PolicyViolation("amount must be greater than zero")
    return d


def validate_payment_amount(amount, balance: Decimal, org: dict) -> Decimal:
    amt = money(amount)
    cap = Decimal(str(org["payments"]["max_single_payment"]))
    if amt > balance:
        raise PolicyViolation(f"amount ${amt} is more than the balance due ${balance}", {"max_amount": str(balance)})
    if amt > cap:
        raise PolicyViolation(f"single payments are capped at ${cap}", {"max_amount": str(cap)})
    return amt


def validate_payment_plan(installments: list[dict], balance: Decimal, today: date, org: dict,
                          has_active_plan: bool) -> list[dict]:
    rules = org["payment_plans"]
    allowed = {
        "max_installments": rules["max_installments"],
        "final_due_by": (today + timedelta(days=rules["max_days_to_final_installment"])).isoformat(),
        "min_first_installment": str((balance * Decimal(str(rules["min_first_installment_pct"]))).quantize(Decimal("0.01"))),
        "total_must_equal": str(balance),
    }
    if has_active_plan:
        raise PolicyViolation("resident already has an active payment plan", allowed)
    if balance <= 0:
        raise PolicyViolation("there is no balance due", allowed)
    if not installments:
        raise PolicyViolation("a plan needs at least one installment", allowed)
    if len(installments) > rules["max_installments"]:
        raise PolicyViolation(f"plans are limited to {rules['max_installments']} installments; longer plans need manager approval", allowed)
    clean, prev = [], today - timedelta(days=1)
    for inst in installments:
        amt = money(inst.get("amount"))
        try:
            due = date.fromisoformat(str(inst.get("due_date")))
        except ValueError:
            raise PolicyViolation(f"due_date '{inst.get('due_date')}' must be YYYY-MM-DD", allowed) from None
        if due < today or due <= prev:
            raise PolicyViolation("installment dates must be today or later and in increasing order", allowed)
        prev = due
        clean.append({"amount": str(amt), "due_date": due.isoformat()})
    if date.fromisoformat(clean[-1]["due_date"]) > date.fromisoformat(allowed["final_due_by"]):
        raise PolicyViolation(f"the final installment must be due by {allowed['final_due_by']}", allowed)
    total = sum(Decimal(i["amount"]) for i in clean)
    if total != balance:
        raise PolicyViolation(f"installments add up to ${total} but the balance is ${balance}", allowed)
    if Decimal(clean[0]["amount"]) < Decimal(allowed["min_first_installment"]):
        raise PolicyViolation(f"the first installment must be at least ${allowed['min_first_installment']}", allowed)
    return clean
