"""Campaign compliance, audit integrity, Twilio webhooks, the HTTP API and the Claude adapter."""
import base64
import hashlib
import hmac
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from voiceops import campaigns, clock
from voiceops.agent.llm import ClaudeModel
from voiceops.integrations import twilio
from voiceops.server import create_app

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def unfreeze():
    yield
    clock.freeze(None)


def test_collections_campaign_respects_consent_disputes_and_quiet_hours(rt):
    clock.freeze(datetime.now(PT).replace(hour=18, minute=30))  # 18:30 PT = 21:30 ET
    decisions = {d["subject_id"]: d for d in campaigns.run_campaign(rt, "collections", "sms")}
    assert decisions["R-1001"]["decision"] == "sent"
    assert "consent" in decisions["R-1003"]["reason"]          # opted out of SMS
    assert "quiet hours" in decisions["R-1004"]["reason"]      # East coast resident
    assert "dispute" in decisions["R-1005"]["reason"]
    assert "R-1002" not in decisions                           # nothing owed
    assert rt.s.store.q("SELECT COUNT(*) n FROM outbox")[0]["n"] == 1


def test_frequency_cap(rt):
    clock.freeze(datetime.now(PT).replace(hour=11))
    for _ in range(3):
        campaigns.run_campaign(rt, "renewal", "sms")
        rt.s.store.x("UPDATE conversations SET status='resolved'")
    d = campaigns.run_campaign(rt, "renewal", "sms")[0]
    assert d["decision"] == "skipped" and "frequency cap" in d["reason"]


def test_audit_chain_detects_tampering(rt):
    conv = rt.conversation_for("voice", "+12065550101")
    rt.handle_turn(conv["id"], "voice", "This is Maria Lopez in unit 4B")
    assert rt.s.store.verify_audit_chain()["valid"]
    rt.s.store.x("UPDATE audit_log SET outcome='tampered' WHERE id=1")
    assert not rt.s.store.verify_audit_chain()["valid"]


def test_twilio_signature():
    params = {"From": "+12065550101", "SpeechResult": "hi"}
    url = "https://example.ngrok.app/twilio/voice/gather?conv=C-1"
    signed = url + "From+12065550101" + "SpeechResulthi"  # params sorted by key, concatenated
    sig = base64.b64encode(hmac.new(b"tok", signed.encode(), hashlib.sha1).digest()).decode()
    assert twilio.valid_signature("tok", url, params, sig)
    assert not twilio.valid_signature("tok", url, {**params, "SpeechResult": "yes"}, sig)


def test_http_api_and_twilio_voice_flow(rt):
    api = TestClient(create_app(rt))
    r = api.post("/twilio/voice", data={"From": "+12065550101"})
    assert "<Gather" in r.text and "recorded" in r.text
    conv = rt.s.store.q("SELECT id FROM conversations")[0]["id"]
    r = api.post(f"/twilio/voice/gather?conv={conv}", data={"SpeechResult": "This is Maria Lopez in unit 4B"})
    assert "verified" in r.text
    r = api.post(f"/twilio/voice/gather?conv={conv}", data={"SpeechResult": "I want to talk to a person"})
    assert "<Dial>" in r.text
    # SMS on the same phone number lands in the same (now escalated) case.
    r = api.post("/api/channels/sms/inbound", json={"from": "+12065550101", "body": "hello?"})
    assert r.json()["conversation_id"] == conv and r.json()["path"] == "escalated_case"
    m = api.get("/api/metrics").json()
    assert m["escalations_by_category"] == {"human_request": 1}
    assert api.get("/api/audit").json()["chain"]["valid"]


def test_claude_adapter_request_shape():
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Hi there"),
                         SimpleNamespace(type="tool_use", id="toolu_1", name="search_policy", input={"query": "pets"})],
                stop_reason="tool_use", model="claude-opus-5-5",
                usage=SimpleNamespace(input_tokens=1000, output_tokens=50, cache_read_input_tokens=800, cache_creation_input_tokens=0))

    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    m = ClaudeModel("claude-opus-5-5", "low", client=fake)
    resp = m.create([{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}], [{"role": "user", "content": "hi"}], [])
    assert captured["thinking"] == {"type": "adaptive"} and captured["output_config"] == {"effort": "low"}
    assert captured["fallbacks"] == "default" and "temperature" not in captured
    assert [b.type for b in resp.blocks] == ["text", "tool_use"]
    assert round(resp.usage.cost("claude-opus-5-5"), 6) == round((1000 * 4 + 50 * 20 + 800 * 0.2) / 1e6, 6)
