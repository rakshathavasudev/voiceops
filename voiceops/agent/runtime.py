"""The agent runtime: one call to handle_turn per inbound utterance on any channel.

Order of operations for a turn:
  1. redact sensitive numbers, persist the user message
  2. deterministic pre-checks (escalated case, declined confirmation, hard triggers,
     turn budget) that never reach the model
  3. the model/tool loop with per-turn budgets, every call traced
  4. output guard, persist reply, record turn metrics
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from voiceops import clock
from voiceops.agent import prompts, tools
from voiceops.agent.guardrails import hard_trigger, is_negative, redact
from voiceops.agent.llm import Usage
from voiceops.integrations.pms_client import PMSError

MAX_MODEL_CALLS = 6
MAX_TOOL_CALLS = 8


@dataclass
class Services:
    store: Any
    pms: Any
    messenger: Any
    tracer: Any
    org_id: str
    org: dict
    model: Any
    settings: Any = None


@dataclass
class TurnResult:
    conversation_id: str
    turn_id: str
    trace_id: str
    reply: str
    channel: str
    escalated: bool = False
    handoff: dict | None = None
    blocked: list[dict] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    path: str = "model"  # "model" | "hard_trigger" | "escalated_case" | "guard"


class AgentRuntime:
    def __init__(self, services: Services) -> None:
        self.s = services

    # ---------------- identity resolution & conversations ----------------

    def resolve_subject(self, channel: str, address: str | None) -> tuple[str, str | None, str, list[str]]:
        """Map a phone number / email to a resident or prospect. Matching contact info identifies,
        it does not verify: residents still pass verify_identity before account access."""
        if not address:
            return "resident", None, "Unknown caller", []
        key = "email" if "@" in address else "phone"
        residents = self.s.pms.find_residents(**{key: address})
        if residents:
            r = residents[0]
            return "resident", r.ResidentID, f"{r.FirstName} {r.LastName}", [r.Phone, r.Email or ""]
        prospects = self.s.pms.find_prospects(**{key: address})
        if prospects:
            p = prospects[0]
            return "prospect", p.ProspectID, f"{p.FirstName} {p.LastName}", [p.Phone, p.Email or ""]
        return "resident", None, "Unknown caller", [address]

    def conversation_for(self, channel: str, address: str | None, purpose: str = "inbound") -> dict:
        """Find the open case for this contact on any channel, or open one. The contact index lets an
        SMS reply land in the same conversation as the phone call, without a PMS round trip."""
        store = self.s.store
        if address:
            rows = store.q("SELECT c.* FROM contact_index ci JOIN conversations c ON c.id=ci.conversation_id "
                           "WHERE ci.address=? AND c.status IN ('active','escalated') ORDER BY c.updated_at DESC LIMIT 1",
                           address.lower())
            if rows:
                if purpose != "inbound" and rows[0]["purpose"] == "inbound":
                    store.update_conversation(rows[0]["id"], purpose=purpose)
                return store.get_conversation(rows[0]["id"])
        try:
            stype, sid, name, contacts = self.resolve_subject(channel, address)
        except PMSError:
            # System of record down: still answer, as an unidentified caller who can verify later.
            stype, sid, name, contacts = "resident", None, "Unknown caller", [address] if address else []
        if sid is None:
            # Anonymous inbound: a fresh case; it binds to a resident once verified.
            conv = store.open_conversation(self.s.org_id, stype, f"anon-{uuid.uuid4().hex[:6]}", name, purpose)
            store.update_conversation(conv["id"], subject_id=None)
        else:
            conv = store.open_conversation(self.s.org_id, stype, sid, name, purpose)
        for c in contacts:
            if c:
                store.x("INSERT OR REPLACE INTO contact_index(address, conversation_id) VALUES (?,?)", c.lower(), conv["id"])
        return store.get_conversation(conv["id"])

    def opening_message(self, conv: dict, channel: str) -> str:
        org = self.s.org
        first = (conv["display_name"] or "there").split()[0]
        purpose = conv["purpose"]
        if channel == "voice":
            extra = f" {org['collections_disclosure']}" if purpose == "collections" else ""
            return (f"Hi, this is {org['agent_name']}, a virtual assistant calling from {org['display_name']}. "
                    f"{org['recording_disclosure']}{extra} Am I speaking with {first}?")
        stop = " Reply STOP to opt out."
        body = {
            "collections": f"Hi {first}, this is {org['agent_name']} from {org['display_name']}. {org['collections_disclosure']} Reply here to review your account and payment options, or call {org['office_phone']}.",
            "renewal": f"Hi {first}, it's {org['agent_name']} from {org['display_name']}. Your lease renewal options are ready. Reply here and I can walk you through them.",
            "lead_followup": f"Hi {first}, it's {org['agent_name']} from {org['display_name']}. Thanks for your interest! Want to book a tour? Reply with a day that works.",
            "application_followup": f"Hi {first}, it's {org['agent_name']} from {org['display_name']}. Your application is saved. Any questions I can answer, or would you like to tour first?",
        }.get(purpose, f"Hi {first}, it's {org['agent_name']} from {org['display_name']}.")
        return body + stop

    def start_outbound(self, conv: dict, channel: str) -> str:
        msg = self.opening_message(conv, channel)
        self.s.store.add_message(conv["id"], None, channel, "assistant", msg)
        self.s.store.update_conversation(conv["id"], last_channel=channel)
        return msg

    # ---------------- the turn ----------------

    def handle_turn(self, conversation_id: str, channel: str, text: str) -> TurnResult:
        s = self.s
        turn_id = "turn-" + uuid.uuid4().hex[:10]
        started = time.perf_counter()
        with s.tracer.span("turn", kind="turn", new_trace=True, conversation_id=conversation_id, channel=channel) as root:
            from voiceops.tracing import current_trace_id
            trace_id = current_trace_id()
            conv = s.store.get_conversation(conversation_id)
            turn_index = conv["turn_count"] + 1
            s.store.update_conversation(conversation_id, turn_count=turn_index, last_channel=channel)
            conv = s.store.get_conversation(conversation_id)
            clean, found = redact(text)
            s.store.add_message(conversation_id, turn_id, channel, "user", clean)
            flags = []
            if found:
                flags.append(f"user shared sensitive data ({', '.join(found)}); it was redacted. Tell them not to share it and offer a secure link.")
                s.store.audit(s.org_id, "system", "pii.redacted", conv["subject_id"] or "unknown", {"kinds": found, "channel": channel}, "success", trace_id=trace_id)
            ctx = tools.ToolContext(s, conv, turn_id, turn_index, channel, clean)
            usage, model_name = Usage(), getattr(s.model, "model", "scripted")
            llm_calls, tool_names, tool_errors = 0, [], 0
            handoff = None

            result_path = "model"
            reply = self._precheck(ctx, flags)
            if reply is not None:
                result_path = "escalated_case" if reply.startswith("\x00") else "hard_trigger"
                reply = reply.lstrip("\x00")
            else:
                try:
                    reply, llm_calls, tool_names, tool_errors = self._model_loop(ctx, flags, usage)
                    reply, guarded = self._output_guard(ctx, reply)
                    if guarded:
                        result_path = "guard"
                except Exception as exc:  # model outage, malformed response: degrade to a human, never to silence
                    root.set(error=f"{type(exc).__name__}: {exc}")
                    tools._escalate(ctx, "technical", f"agent error: {type(exc).__name__}", "The assistant hit an internal error mid-conversation.")
                    reply = f"I'm sorry, I'm having trouble on my end. I've asked our leasing office to follow up, or you can call {s.org['office_phone']}."
                    result_path = "error"

            conv = s.store.get_conversation(conversation_id)
            escalated = conv["status"] == "escalated"
            if escalated:
                esc = s.store.q("SELECT * FROM escalations WHERE conversation_id=? ORDER BY id DESC LIMIT 1", conversation_id)
                if esc:
                    handoff = {"category": esc[0]["category"], "queue": s.org["escalation"]["queue"],
                               "transfer_to": s.org["office_phone"] if channel == "voice" else None}
            s.store.add_message(conversation_id, turn_id, channel, "assistant", reply)
            latency = (time.perf_counter() - started) * 1000
            cost = usage.cost(model_name)
            root.set(turn_id=turn_id, path=result_path, llm_calls=llm_calls, tools=tool_names, escalated=escalated,
                     cost_usd=round(cost, 6), input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
            s.store.x("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", turn_id, conversation_id, channel,
                      clock.now().isoformat(), round(latency, 1), llm_calls, usage.input_tokens, usage.output_tokens,
                      usage.cache_read_tokens, cost, model_name, int(escalated), len(ctx.blocked), tool_errors)
        return TurnResult(conversation_id, turn_id, trace_id, reply, channel, escalated, handoff, ctx.blocked,
                          tool_names, round(latency, 1), round(cost, 6), result_path)

    def _precheck(self, ctx: tools.ToolContext, flags: list[str]) -> str | None:
        s, conv, org, channel = self.s, ctx.conv, self.s.org, ctx.channel
        if conv["status"] == "escalated":
            return "\x00" + ("A member of our leasing office has your case and will follow up shortly. "
                             f"If it's urgent, call {org['office_phone']}.")
        if conv["pending_confirmation"] and is_negative(ctx.user_text):
            pending = json.loads(conv["pending_confirmation"])
            ctx.update(pending_confirmation=None)
            s.store.add_event(conv["id"], ctx.turn_id, "confirmation_declined", {"id": pending["id"], "action": pending["action"]})
            flags.append(f"The user declined the pending {pending['action']}; it was cancelled and nothing was submitted.")
        trig = hard_trigger(ctx.user_text, channel)
        if trig:
            with s.tracer.span("guardrail.hard_trigger", kind="guardrail", category=trig.category, reason=trig.reason):
                return self._handle_trigger(ctx, trig)
        if ctx.turn_index > org["escalation"]["max_turns"]:
            tools._escalate(ctx, "other", "turn budget exceeded", "Conversation ran past the turn budget without resolution.")
            return f"Let me get a teammate to help finish this. Someone from the leasing office will follow up, or call {org['office_phone']}."
        return None

    def _handle_trigger(self, ctx: tools.ToolContext, trig) -> str:
        org, channel = self.s.org, ctx.channel
        summary = f"Auto-escalated ({trig.reason}). Last message: {ctx.user_text[:300]}"
        if trig.category == "emergency":
            tools._escalate(ctx, "emergency", trig.reason, summary)
            return (f"That sounds like an emergency. Please call our 24/7 emergency maintenance line at {org['emergency_maintenance_phone']} now"
                    f"{', and 911 first if there is fire or a gas smell' if channel != 'email' else ''}. I've also alerted the on-call team.")
        if trig.category == "opt_in":
            if ctx.conv["subject_id"]:
                self.s.store.set_consent(ctx.conv["subject_id"], "sms", "granted", "keyword:sms")
                ctx.audit("consent.granted", {"channel": "sms", "source": trig.reason}, "success")
            return f"You're resubscribed to {org['display_name']} texts. Reply STOP at any time to opt out."
        if trig.category == "opt_out":
            target = {"sms": "sms", "email": "email"}.get(channel, "voice")
            if ctx.conv["subject_id"]:
                self.s.store.set_consent(ctx.conv["subject_id"], target, "revoked", f"keyword:{channel}")
                ctx.audit("consent.revoked", {"channel": target, "source": trig.reason}, "success")
            ctx.update(status="closed", pending_confirmation=None)
            if channel == "sms":
                return f"You're unsubscribed from {org['display_name']} texts and won't receive more. Reply START to resubscribe."
            return "Understood. We've removed you from our call list. Have a good day."
        tools._escalate(ctx, trig.category, trig.reason, summary)
        if trig.category == "dispute":
            return ("I understand you believe that's not right. I've flagged it for our leasing office to review, and nothing will be "
                    "charged or changed in the meantime. A team member will contact you within one business day.")
        if channel == "voice":
            return "Of course. I'm transferring you to our leasing office now, and I've passed along what we discussed."
        return f"I've passed this to our leasing office and a team member will reply here soon. You can also call {org['office_phone']}."

    def _model_loop(self, ctx: tools.ToolContext, flags: list[str], usage: Usage) -> tuple[str, int, list[str], int]:
        s = self.s
        static = prompts.system_prompt(s.org)
        tool_names: list[str] = []
        tool_errors = 0
        conv = s.store.get_conversation(ctx.conv["id"])
        messages = prompts.history_messages(s.store.messages(conv["id"]), conv["purpose"])
        llm_calls = 0
        text_out = ""
        while llm_calls < MAX_MODEL_CALLS:
            ctx.refresh()
            dynamic = prompts.case_state(ctx.conv, ctx.channel, s.org, s.store.events(ctx.conv["id"]), flags, ctx.verified())
            system = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
                      {"type": "text", "text": dynamic}]
            with s.tracer.span("llm", kind="llm", model=getattr(s.model, "model", "scripted"), call=llm_calls + 1) as sp:
                resp = s.model.create(system, messages, tools.TOOL_SPECS)
                sp.set(stop_reason=resp.stop_reason, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                       cache_read_tokens=resp.usage.cache_read_tokens, model_latency_ms=round(resp.latency_ms, 1))
            llm_calls += 1
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                setattr(usage, k, getattr(usage, k) + getattr(resp.usage, k))
            text_out = " ".join(b.text for b in resp.blocks if b.type == "text").strip()
            calls = [b for b in resp.blocks if b.type == "tool_use"]
            if resp.stop_reason == "refusal":
                tools._escalate(ctx, "technical", "model refusal", "The model declined to continue; human follow-up needed.")
                return (f"I'm not able to help with that here, so I've asked our leasing office to follow up. You can also call {s.org['office_phone']}.",
                        llm_calls, tool_names, tool_errors)
            if not calls:
                break
            messages.append({"role": "assistant", "content": resp.raw_content})
            results = []
            for call in calls:
                if len(tool_names) >= MAX_TOOL_CALLS:
                    out, err = {"error": "tool budget for this turn exhausted; answer the user now"}, True
                else:
                    tool_names.append(call.name)
                    with s.tracer.span(f"tool {call.name}", kind="tool", args=call.input) as sp:
                        out, err = tools.execute(ctx, call.name, call.input)
                        sp.set(result=out)
                        if err:
                            sp.error(out.get("blocked_by") or out.get("error", "error"))
                tool_errors += int(err)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(out, default=str), "is_error": err})
            messages.append({"role": "user", "content": results})
            if ctx.conv["status"] == "escalated" and not text_out:
                continue
        if not text_out:
            text_out = "Sorry, I didn't catch that. Could you say it another way?"
        return text_out, llm_calls, tool_names, tool_errors

    def _output_guard(self, ctx: tools.ToolContext, reply: str) -> tuple[str, bool]:
        """Last check before anything is said: no card data, and no account figures to an unverified resident."""
        reply, found = redact(reply)
        ctx.refresh()
        if ctx.conv["subject_type"] == "resident" and ctx.conv["subject_id"] and not ctx.verified():
            try:
                ledger = self.s.pms.ledger(ctx.conv["subject_id"])
                lease = self.s.pms.lease(ctx.conv["subject_id"])
            except PMSError:
                # Can't fetch the figures to compare against, so fail closed on any dollar amount.
                if re.search(r"\$\s?\d", reply):
                    return "Before I can discuss account details, I need to verify your identity. What's your last name and unit number?", True
                return reply, bool(found)
            secrets = {f"{v:,.2f}" for v in (ledger.BalanceDue, lease.MonthlyRent) if v} | {f"{v:.2f}" for v in (ledger.BalanceDue, lease.MonthlyRent) if v}
            secrets |= {s.removesuffix(".00") for s in secrets}
            digits = reply.replace("$", "")
            if any(re.search(rf"(?<![\d,.]){re.escape(x)}(?![\d])", digits) for x in secrets):
                ctx.blocked.append({"tool": "output", "guardrail": "disclosure", "reason": "account figure in reply to unverified resident"})
                self.s.store.add_event(ctx.conv["id"], ctx.turn_id, "guardrail_blocked", {"tool": "output", "guardrail": "disclosure"})
                return "Before I can discuss account details, I need to verify your identity. What's your last name and unit number?", True
        return reply, bool(found)
