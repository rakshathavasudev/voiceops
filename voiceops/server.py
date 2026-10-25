"""HTTP surface: channel webhooks (Twilio voice/SMS, email, web), the operator console API,
and admin hooks for demos. Run everything with `python -m voiceops`."""
from __future__ import annotations

import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from voiceops import campaigns, clock, metrics
from voiceops.agent.runtime import AgentRuntime
from voiceops.bootstrap import build_runtime
from voiceops.config import ROOT, settings as load_settings
from voiceops.integrations import twilio
from voiceops.integrations.pms_client import PMSError
from voiceops.messaging import ConsentError

WEB = ROOT / "web"


class TurnIn(BaseModel):
    channel: str = "web"
    text: str
    address: str | None = None  # caller ID / sender; identifies but never verifies
    conversation_id: str | None = None


class StartIn(BaseModel):
    channel: str
    address: str | None = None
    purpose: str = "inbound"


class SmsIn(BaseModel):
    from_: str = Field(alias="from")
    body: str


class EmailIn(BaseModel):
    sender: str
    subject: str = ""
    text: str


def create_app(rt: AgentRuntime | None = None) -> FastAPI:
    rt = rt or build_runtime(load_settings())
    s = rt.s
    app = FastAPI(title="VoiceOps agent platform")
    app.state.rt = rt

    def turn_payload(res) -> dict:
        return res.__dict__

    def inbound_greeting() -> str:
        org = s.org
        return (f"Thanks for calling {org['display_name']}. {org['recording_disclosure']} "
                f"This is {org['agent_name']}, the virtual assistant. How can I help?")

    def conversation_id_for(channel: str, address: str | None) -> str:
        return rt.conversation_for(channel, address)["id"]

    # ---------------- console + web channels ----------------

    @app.get("/")
    def console():
        return FileResponse(WEB / "console.html")

    @app.get("/api/info")
    def info():
        st = s.settings
        return {"provider": "scripted" if getattr(s.model, "model", "") == "scripted" else "claude",
                "model": getattr(s.model, "model", None), "effort": st.effort if st else None, "org": s.org["display_name"]}

    @app.get("/api/subjects")
    def subjects():
        residents = [{"type": "resident", "id": r.ResidentID, "name": f"{r.FirstName} {r.LastName}", "unit": r.UnitID,
                      "phone": r.Phone, "email": r.Email, "flags": r.Flags} for r in s.pms.find_residents()]
        prospects = [{"type": "prospect", "id": p.ProspectID, "name": f"{p.FirstName} {p.LastName}", "phone": p.Phone,
                      "email": p.Email, "status": p.Status} for p in s.pms.find_prospects()]
        return {"subjects": residents + prospects}

    @app.post("/api/conversations")
    def start(body: StartIn):
        conv = rt.conversation_for(body.channel, body.address, body.purpose)
        opening = None
        if not s.store.messages(conv["id"]):
            if body.purpose != "inbound":
                opening = rt.start_outbound(conv, body.channel)
            elif body.channel == "voice":
                opening = inbound_greeting()
                s.store.add_message(conv["id"], None, "voice", "assistant", opening)
        return {"conversation": s.store.get_conversation(conv["id"]), "opening": opening}

    @app.post("/api/turn")
    def turn(body: TurnIn):
        cid = body.conversation_id or conversation_id_for(body.channel, body.address)
        try:
            return turn_payload(rt.handle_turn(cid, body.channel, body.text))
        except PMSError as exc:
            raise HTTPException(502, f"system of record error: {exc}")

    @app.post("/api/channels/sms/inbound")
    def sms_inbound(body: SmsIn):
        """Same path as the Twilio webhook, with JSON for the console's SMS simulator."""
        res = rt.handle_turn(conversation_id_for("sms", body.from_), "sms", body.body)
        _deliver_reply(res, "sms", body.from_)
        return turn_payload(res)

    @app.post("/api/channels/email/inbound")
    def email_inbound(body: EmailIn):
        res = rt.handle_turn(conversation_id_for("email", body.sender), "email", f"{body.subject}\n{body.text}".strip())
        _deliver_reply(res, "email", body.sender)
        return turn_payload(res)

    def _deliver_reply(res, channel: str, to: str) -> None:
        conv = s.store.get_conversation(res.conversation_id)
        s.store.x("INSERT INTO outbox(conversation_id, channel, to_addr, body, status, created_at) VALUES (?,?,?,?,?,?)",
                  conv["id"], channel, to, res.reply, "reply", clock.now().isoformat())

    # ---------------- Twilio voice + SMS ----------------

    async def twilio_params(request: Request) -> dict:
        form = dict(await request.form())
        st = s.settings
        if st and st.twilio_auth_token:
            url = (st.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")) if st.public_base_url else str(request.url)
            if not twilio.valid_signature(st.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "invalid Twilio signature")
        return form

    def twiml(xml: str) -> Response:
        return Response(xml, media_type="application/xml")

    def voice_response(res) -> Response:
        if res.handoff and res.handoff.get("transfer_to"):
            return twiml(twilio.say_and_dial(res.reply, res.handoff["transfer_to"]))
        if s.store.get_conversation(res.conversation_id)["status"] == "closed":
            return twiml(twilio.say_and_hangup(res.reply))
        return twiml(twilio.say_and_gather(res.reply, f"/twilio/voice/gather?conv={res.conversation_id}"))

    @app.post("/twilio/voice")
    async def twilio_voice_incoming(request: Request):
        form = await twilio_params(request)
        conv = rt.conversation_for("voice", form.get("From"))
        greeting = inbound_greeting()
        s.store.add_message(conv["id"], None, "voice", "assistant", greeting)
        return twiml(twilio.say_and_gather(greeting, f"/twilio/voice/gather?conv={conv['id']}"))

    @app.post("/twilio/voice/outbound")
    async def twilio_voice_outbound(request: Request, conv: str):
        await twilio_params(request)
        msgs = [m for m in s.store.messages(conv) if m["role"] == "assistant"]
        opening = msgs[0]["text"] if msgs else rt.opening_message(s.store.get_conversation(conv), "voice")
        return twiml(twilio.say_and_gather(opening, f"/twilio/voice/gather?conv={conv}"))

    @app.post("/twilio/voice/gather")
    async def twilio_voice_gather(request: Request, conv: str, silence: int = 0):
        form = await twilio_params(request)
        speech = form.get("SpeechResult", "")
        if silence or not speech:
            return twiml(twilio.say_and_gather("Sorry, I didn't hear anything. How can I help?", f"/twilio/voice/gather?conv={conv}"))
        return voice_response(rt.handle_turn(conv, "voice", speech))

    @app.post("/twilio/sms")
    async def twilio_sms(request: Request):
        form = await twilio_params(request)
        res = rt.handle_turn(conversation_id_for("sms", form.get("From")), "sms", form.get("Body", ""))
        return twiml(twilio.message(res.reply))

    # ---------------- operations / observability ----------------

    @app.get("/api/conversations")
    def conversations():
        return {"conversations": s.store.q("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100")}

    @app.get("/api/conversations/{cid}")
    def conversation(cid: str):
        conv = s.store.get_conversation(cid)
        conv["verified"] = bool(conv["verified_until"]) and datetime.fromisoformat(conv["verified_until"]) > clock.now()
        return {"conversation": conv, "messages": s.store.messages(cid, 200),
                "events": s.store.events(cid, 100),
                "turns": s.store.q("SELECT * FROM turns WHERE conversation_id=? ORDER BY started_at", cid),
                "escalations": s.store.q("SELECT * FROM escalations WHERE conversation_id=?", cid)}

    @app.get("/api/traces/{trace_id}")
    def trace(trace_id: str):
        return {"spans": s.tracer.trace(trace_id)}

    @app.get("/api/traces")
    def traces(conversation_id: str | None = None, limit: int = 20):
        rows = s.store.q("SELECT trace_id, start_ts, duration_ms, attrs FROM spans WHERE kind='turn' ORDER BY id DESC LIMIT 200")
        out = []
        for r in rows:
            attrs = json.loads(r["attrs"])
            if conversation_id and attrs.get("conversation_id") != conversation_id:
                continue
            out.append({**r, "attrs": attrs})
        return {"traces": out[:limit]}

    @app.get("/api/audit")
    def audit(limit: int = 50):
        rows = s.store.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", limit)
        return {"entries": [{**r, "details": json.loads(r["details"])} for r in rows], "chain": s.store.verify_audit_chain()}

    @app.get("/api/outbox")
    def outbox(limit: int = 30):
        return {"messages": s.store.q("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", limit)}

    @app.get("/api/escalations")
    def escalations():
        return {"escalations": s.store.q("SELECT e.*, c.display_name FROM escalations e JOIN conversations c ON c.id=e.conversation_id ORDER BY e.id DESC")}

    @app.get("/api/writes")
    def writes():
        return {"writes": s.store.q("SELECT * FROM pending_writes ORDER BY id DESC LIMIT 50"), "breaker": s.pms.breaker_state()}

    @app.post("/api/writes/drain")
    def drain():
        results = s.pms.drain_outbox()
        for r in results:
            if r["status"] == "committed" and r.get("conversation_id"):
                s.store.audit(s.org_id, "worker:outbox", f"{r['op']}.committed", r["conversation_id"], {"response": r["response"], "deferred": True}, "committed", r["key"])
                notify_deferred_commit(r)
        return {"results": results}

    def notify_deferred_commit(r: dict) -> None:
        """Keep the promise the agent made when the write was queued: confirm by text once it lands."""
        conv = s.store.get_conversation(r["conversation_id"])
        label = r["op"].replace("_", " ")
        url = (r.get("response") or {}).get("Url")
        msg = f"{s.org['display_name']}: your {label} has been processed." + (f" Pay securely here: {url}" if url else "") + " Reply here with any questions."
        s.store.add_message(conv["id"], None, "sms", "assistant", msg)
        s.store.add_event(conv["id"], None, f"{r['op']}_committed", {"deferred": True, "response": r.get("response")})
        try:
            subject = s.pms.get_resident(conv["subject_id"]) if conv["subject_type"] == "resident" else s.pms.get_prospect(conv["subject_id"])
            s.messenger.send(conv["id"], conv["subject_id"], "sms", subject.Phone, msg, transactional=True)
        except (PMSError, ConsentError):
            pass

    @app.get("/api/metrics")
    def get_metrics():
        return metrics.compute(s.store)

    @app.post("/api/campaigns/{name}/run")
    def run_campaign(name: str, channel: str = "sms"):
        try:
            return {"decisions": campaigns.run_campaign(rt, name, channel)}
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/pms/state")
    def pms_state():
        return s.pms.http.get(f"{s.pms.base_url}/_admin/state").json()

    @app.post("/api/pms/faults")
    async def pms_faults(request: Request):
        body = await request.json()
        s.pms._open_until = 0
        return s.pms.http.post(f"{s.pms.base_url}/_admin/faults", json=body).json()

    @app.post("/api/payments/{link_id}/complete")
    def complete_payment(link_id: str):
        """Simulates the payment processor's webhook after the resident pays via the link."""
        r = s.pms.http.post(f"{s.pms.base_url}/api/v1/payment-links/{link_id}/complete").json()
        s.store.audit(s.org_id, "webhook:payments", "payment.completed", link_id, r, "success")
        return r

    @app.post("/api/reset")
    def reset():
        s.store.reset()
        s.pms.http.post(f"{s.pms.base_url}/_admin/reset")
        s.pms._open_until, s.pms._consecutive_failures = 0, 0
        return {"ok": True}

    return app
