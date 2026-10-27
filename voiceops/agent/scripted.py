"""A deterministic stand-in for the LLM.

It speaks the same interface as ClaudeModel (system, messages, tools -> tool_use / text
blocks), driven by keyword rules. It exists so that:
  * the demo runs end to end with no API key or network
  * the eval suite can run in CI as a regression test of everything *around* the model
    (tools, guardrails, integrations, state), with zero variance and zero cost
It is intentionally simple; run the same evals with VOICEOPS_MODEL_PROVIDER=claude to
measure the real agent.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from voiceops.agent.guardrails import is_affirmative
from voiceops.agent.llm import Block, ModelResponse, Usage

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "twelve": 12}
_NOT_NAMES = {"in", "from", "at", "and", "unit", "apartment", "calling", "here", "the"}

INTENTS = [
    ("injection", r"ignore (all |your |previous |prior )*(instructions|rules)|mark .{0,30}(paid|zero)|system prompt|you are now"),
    # Policy nouns win over "pay": "what's the late fee if I pay late?" is a question, not a payment request.
    ("policy", r"late fee|\bfees?\b|\bpets?\b|parking|office hours|\bpolicy\b|application fee"),
    ("plan", r"\bplan\b|split|installment|can'?t pay|cannot pay|over \w+ months|in (two|three|four|\d) (payments|parts)|(two|three|four|\d+) payments"),
    ("pay", r"\bpay\b|payment link|\blink\b|in full"),
    ("balance", r"balance|how much|\bowe\b|what do i owe|account"),
    ("renewal", r"renew|renewal|(6|six|12|twelve)[ -]month|\bthe (6|12)\b|lease (offer|option)"),
    ("tour", r"\btour|visit|come (see|by)|schedule|\bbook\b|see the (apartment|unit|place)"),
    ("policy", r"fee|\bpets?\b|\bdog|\bcat\b|parking|office|hours|\bopen\b|policy|application|ways to pay|how do i|how can i"),
    ("send", r"text me|send me|email me"),
    ("bye", r"thank|bye|that'?s all|that is all|nothing else|have a good"),
]


def _intent(text: str) -> str | None:
    for name, pattern in INTENTS:
        if re.search(pattern, text, re.I):
            return name
    return None


def _extract_identity(text: str) -> tuple[str | None, str | None]:
    unit = re.search(r"(?:unit|apartment|apt\.?|#)\s*([0-9]+[a-z]?)\b", text, re.I)
    last = re.search(r"last name(?: is|'s)?\s+([A-Za-z'-]+)", text, re.I)
    name = None
    if last:
        name = last.group(1)
    else:
        m = re.search(r"(?:this is|i'm|i am|name is|it's|my name's)\s+([A-Za-z'-]+)(?:\s+([A-Za-z'-]+))?", text, re.I)
        if m:
            name = m.group(2) if m.group(2) and m.group(2).lower() not in _NOT_NAMES else m.group(1)
        else:
            m = re.search(r"\b([A-Za-z'-]+)\s+([A-Za-z'-]+)\W+(?:in\s+)?(?:unit|apartment|apt)", text, re.I)
            if m:
                name = m.group(2)
    return name, unit.group(1).upper() if unit else None


def _count(text: str) -> int | None:
    m = re.search(r"\b(\d+|one|two|three|four|five|six|twelve)\s+(payments|installments|parts|months|month)", text, re.I)
    if not m:
        return None
    tok = m.group(1).lower()
    return int(tok) if tok.isdigit() else NUMBER_WORDS[tok]


def _amount(text: str) -> str | None:
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d{2})?)|(\d[\d,]*(?:\.\d{2})?)\s*dollars", text, re.I)
    if not m:
        return None
    return (m.group(1) or m.group(2)).replace(",", "")


def _plan(balance: Decimal, n: int, today: date) -> list[dict]:
    base = (balance / n).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    first = balance - base * (n - 1)
    gap = 28 if n <= 3 else 30
    return [{"amount": str(first if i == 0 else base), "due_date": (today + timedelta(days=3 + gap * i)).isoformat()} for i in range(n)]


class ScriptedModel:
    model = "scripted"

    def create(self, system: list[dict], messages: list[dict], tools: list[dict]) -> ModelResponse:
        dynamic = system[-1]["text"]
        state = json.loads(dynamic.split("<case_state>", 1)[1].split("</case_state>", 1)[0])
        text, prev_text, done = self._turn(messages)
        blocks = self._decide(state, text, prev_text, done)
        chars = sum(len(b["text"]) for b in system) + sum(len(json.dumps(m["content"], default=str)) for m in messages)
        usage = Usage(input_tokens=chars // 4, output_tokens=sum(len(json.dumps(b)) for b in blocks) // 4)
        norm = [Block(b["type"], text=b.get("text", ""), id=b.get("id", ""), name=b.get("name", ""), input=b.get("input", {})) for b in blocks]
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return ModelResponse(blocks, norm, stop, usage, "scripted", 0.0)

    @staticmethod
    def _turn(messages: list[dict]) -> tuple[str, str, dict]:
        """Current user text, the previous user text, and tool results gathered so far this turn."""
        idx = max(i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str))
        strip = lambda s: re.sub(r"^\[\w+\]\s*", "", s.strip().split("\n")[-1])
        prev = [m for m in messages[:idx] if m["role"] == "user" and isinstance(m["content"], str)]
        done: dict = {}
        pending_calls: dict = {}
        for m in messages[idx + 1:]:
            for b in m["content"]:
                if b["type"] == "tool_use":
                    pending_calls[b["id"]] = b
                elif b["type"] == "tool_result":
                    call = pending_calls[b["tool_use_id"]]
                    done[call["name"]] = (call["input"], json.loads(b["content"]), b.get("is_error", False))
        return strip(messages[idx]["content"]), strip(prev[-1]["content"]) if prev else "", done

    def _decide(self, st: dict, text: str, prev_text: str, done: dict) -> list[dict]:
        def tool(name, **inp):
            return [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": name, "input": inp}]

        def say(msg):
            return [{"type": "text", "text": msg}]

        t = text.lower()
        flags = " ".join(st["flags"])
        verified = st["identity_verified"]
        pending = st["pending_confirmation"]

        # ---- results of writes ----
        if "commit_confirmed_action" in done:
            _, r, err = done["commit_confirmed_action"]
            if err:
                return say("I wasn't able to complete that yet. Could you confirm with a clear yes if you'd like me to go ahead?")
            if r["status"] == "queued":
                return say("Our property system is running slow, so I've saved your request and it will go through automatically. "
                           "You'll get a text confirmation once it's processed.")
            return say({
                "payment_link": "Done. I've sent the secure payment link. It's good for 24 hours. Anything else I can help with?",
                "payment_plan": "You're all set. Your payment plan is in place, and you'll get a reminder before each due date. Anything else?",
                "lease_renewal": "Great, your renewal is submitted. You'll get the addendum to e-sign within 7 days. Anything else?",
                "tour_booking": "You're booked! You'll get a text reminder the day before. Anything else?",
            }[r["action"]])
        if "escalate_to_human" in done:
            return say("I've passed this to our leasing office with the details, and someone will follow up shortly.")
        for name in ("prepare_payment_link", "prepare_payment_plan", "prepare_lease_renewal", "prepare_tour_booking"):
            if name in done:
                _, r, err = done[name]
                if err:
                    if r.get("blocked_by") == "policy" and "allowed" in r:
                        a = r["allowed"]
                        return say(f"I can't set that up: {r['error']}. I can split it into up to {a.get('max_installments', 3)} payments "
                                   f"with the last one due by {a.get('final_due_by')}. Would that work?")
                    return say(f"I can't do that: {r['error']}")
                return say(f"{r['readback']}. Should I go ahead?")

        if "declined" in flags:
            return say("No problem, I've cancelled that and nothing was submitted. What would you like to do instead?")
        if "sensitive data" in flags:
            return say("For your security, please don't share card numbers here. I can send you a secure payment link instead. Would you like that?")

        if pending and is_affirmative(t):
            return tool("commit_confirmed_action", confirmation_id=pending["confirmation_id"])

        intent = _intent(t)
        if intent == "injection":
            return say("I can't change balances or skip account rules, but I can send a secure payment link or set up a payment plan.")

        # ---- identity ----
        if st["subject_type"] == "resident" and not verified:
            if "verify_identity" in done:
                r = done["verify_identity"][1]
                if r.get("locked"):
                    return say("I'm sorry, I wasn't able to verify your identity. A member of our leasing office will follow up with you.")
                if not r.get("verified"):
                    return say("I'm sorry, I couldn't verify that. Could you repeat your last name and unit number?")
            else:
                name, unit = _extract_identity(text)
                if name and unit:
                    return tool("verify_identity", last_name=name, unit_number=unit)
                if intent == "policy":
                    return self._policy(done, text, tool, say)
                return say("I can help with that. First, to protect your account, what's your last name and unit number?")
        if "verify_identity" in done and done["verify_identity"][1].get("verified"):
            # Just verified this turn: pick up what they asked before, or the outbound goal.
            if intent is None and prev_text:
                intent = _intent(prev_text.lower())
            goal = st["conversation_goal"].lower()
            if intent is None:
                intent = "balance" if "collections" in goal else "renewal" if "renewal" in goal else None
            if intent is None:
                return say("Thanks, you're verified. How can I help today?")

        # ---- intents ----
        if intent in ("plan", "pay", "balance"):
            if "get_account_summary" not in done:
                return tool("get_account_summary")
            summary = done["get_account_summary"][1]
            if "error" in summary:
                return say("I'm having trouble reaching your account right now. Can I text you once it's back up?")
            bal = Decimal(summary["balance_due"])
            if bal <= 0:
                return say("Good news, you don't have a balance due right now.")
            if intent == "plan":
                n = _count(t) or (3 if "three" in t or "3" in t else 2 if "two" in t else 3)
                return tool("prepare_payment_plan", installments=_plan(bal, n, date.fromisoformat(summary["today"])))
            if intent == "pay":
                return tool("prepare_payment_link", amount=_amount(text) or str(bal), send_via="email" if "email" in t else "sms")
            lim = summary["payment_plan_limits"]
            return say(f"Your balance is ${bal}, which is {summary['days_past_due']} days past due. I can text you a secure link to pay, "
                       f"or set up a plan of up to {lim['max_installments']} payments. Which would you prefer?")
        if intent == "renewal":
            if "get_renewal_offers" not in done:
                return tool("get_renewal_offers")
            offers = done["get_renewal_offers"][1].get("offers", [])
            pick = next((o for o in offers if re.search(rf"\b{o['term_months']}\b|{'twelve' if o['term_months'] == 12 else 'six'}", t)), None)
            if pick and re.search(r"renew|month|go with|take|want|like", t):
                return tool("prepare_lease_renewal", offer_id=pick["offer_id"])
            opts = " or ".join(f"{o['term_months']} months at ${o['monthly_rent']} a month" for o in offers)
            return say(f"You have two renewal options: {opts}. Which would you like?")
        if intent == "tour" or (st["subject_type"] == "prospect" and re.search(r"tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|\d ?(am|pm)", t)):
            if "get_tour_slots" not in done:
                return tool("get_tour_slots")
            slots = done["get_tour_slots"][1].get("slots", [])
            want_pm = bool(re.search(r"afternoon|2 ?pm|\bpm\b", t))
            want_am = bool(re.search(r"morning|10|\bam\b", t))
            if want_am or want_pm or re.search(r"first|earliest|tomorrow", t):
                pick = next((s for s in slots if (want_pm and s["starts_at"].endswith("14:00")) or (want_am and s["starts_at"].endswith("10:00"))
                             or not (want_am or want_pm)), slots[0])
                return tool("prepare_tour_booking", slot_id=pick["slot_id"])
            return say("I have openings " + ", ".join(s["starts_at"].replace("T", " at ") for s in slots[:3]) + ". Which works for you?")
        if intent == "policy":
            return self._policy(done, text, tool, say)
        if intent == "send":
            if "send_message" not in done:
                return tool("send_message", channel="email" if "email" in t else "sms", text="Summary of your conversation with Harborview: we're here if you need anything else.")
            return say("Sent. Anything else?")
        if intent == "bye":
            return say("You're welcome! Have a great day.")
        return say("I can help with your balance, payments, payment plans, lease renewals, tours, or policy questions. What would you like to do?")

    @staticmethod
    def _policy(done, text, tool, say):
        if "search_policy" not in done:
            return tool("search_policy", query=text)
        results = done["search_policy"][1].get("results", [])
        if not results:
            return say("I don't have that in our handbook, so I'll have the leasing office follow up.")
        first = " ".join(re.split(r"(?<=\.)\s", results[0]["text"])[:2])
        return say(first)
