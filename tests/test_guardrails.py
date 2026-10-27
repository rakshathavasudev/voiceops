"""Guardrails must hold even when the model misbehaves. These tests drive the runtime with a
fake model that issues exactly the tool calls we want, including ones a good model wouldn't."""
import json
from datetime import date

import pytest

from voiceops import clock
from voiceops.agent import guardrails as g

MARIA = "+12065550101"
TODAY = clock.now().date()


def plan_args():
    return {"installments": [
        {"amount": "725.00", "due_date": str(date.fromordinal(TODAY.toordinal() + 3))},
        {"amount": "725.00", "due_date": str(date.fromordinal(TODAY.toordinal() + 30))}]}


def verify():
    return {"tool": "verify_identity", "input": {"last_name": "Lopez", "unit_number": "4B"}}


def test_commit_in_same_turn_as_prepare_is_blocked(fake_model_rt, pms_app):
    rt = fake_model_rt([verify(), {"text": "verified"},
                        {"tool": "prepare_payment_plan", "input": plan_args()},
                        {"tool": "commit_confirmed_action", "input": {"confirmation_id": "__LAST__"}},
                        {"text": "done"}])
    conv = rt.conversation_for("voice", MARIA)
    rt.handle_turn(conv["id"], "voice", "This is Maria Lopez unit 4B")
    # Model "self-confirms" by committing immediately after preparing.
    orig = rt.s.model.create

    def create(system, messages, tools):
        resp = orig(system, messages, tools)
        for b in resp.blocks:
            if b.name == "commit_confirmed_action":
                cid = json.loads(rt.s.store.get_conversation(conv["id"])["pending_confirmation"])["id"]
                b.input["confirmation_id"] = cid
                resp.raw_content[0]["input"]["confirmation_id"] = cid
        return resp

    rt.s.model.create = create
    res = rt.handle_turn(conv["id"], "voice", "yes split it in two and do it")
    assert any(b["guardrail"] == "confirmation" for b in res.blocked)
    assert pms_app.state.pms.q("SELECT COUNT(*) n FROM payment_plans")[0]["n"] == 0


def test_commit_without_affirmative_is_blocked(fake_model_rt, pms_app):
    rt = fake_model_rt([verify(), {"text": "verified"},
                        {"tool": "prepare_payment_plan", "input": plan_args()}, {"text": "readback"},
                        {"tool": "commit_confirmed_action", "input": {"confirmation_id": "X"}}, {"text": "done"}])
    conv = rt.conversation_for("voice", MARIA)
    rt.handle_turn(conv["id"], "voice", "This is Maria Lopez unit 4B")
    rt.handle_turn(conv["id"], "voice", "two payments please")
    cid = json.loads(rt.s.store.get_conversation(conv["id"])["pending_confirmation"])["id"]
    rt.s.model.plan[0]["input"]["confirmation_id"] = cid
    res = rt.handle_turn(conv["id"], "voice", "hmm, what would the dates be?")
    assert [b["guardrail"] for b in res.blocked] == ["confirmation"]
    assert pms_app.state.pms.q("SELECT COUNT(*) n FROM payment_plans")[0]["n"] == 0


def test_account_tools_require_verification(fake_model_rt):
    rt = fake_model_rt([{"tool": "get_account_summary"}, {"text": "Your balance is $1450.00"}])
    conv = rt.conversation_for("voice", MARIA)
    res = rt.handle_turn(conv["id"], "voice", "what's my balance")
    assert res.blocked[0]["guardrail"] == "verification"
    # The output guard also catches the model trying to say the number anyway.
    assert "1450" not in res.reply and res.path == "guard"


def test_policy_limits_enforced_in_code(fake_model_rt):
    six = {"installments": [{"amount": "241.67", "due_date": str(date.fromordinal(TODAY.toordinal() + 3 + 30 * i))} for i in range(6)]}
    rt = fake_model_rt([verify(), {"text": "ok"}, {"tool": "prepare_payment_plan", "input": six}, {"text": "sorry"}])
    conv = rt.conversation_for("voice", MARIA)
    rt.handle_turn(conv["id"], "voice", "This is Maria Lopez unit 4B")
    res = rt.handle_turn(conv["id"], "voice", "six payments")
    assert res.blocked[0]["guardrail"] == "policy"
    assert rt.s.store.get_conversation(conv["id"])["pending_confirmation"] is None


def test_tools_cannot_reach_other_residents(fake_model_rt):
    # No tool accepts a resident ID; the only way to bind a subject is verification against caller ID.
    rt = fake_model_rt([{"tool": "verify_identity", "input": {"last_name": "Chen", "unit_number": "12A"}}, {"text": "no"}])
    conv = rt.conversation_for("voice", MARIA)
    rt.handle_turn(conv["id"], "voice", "I'm James Chen in 12A")
    c = rt.s.store.get_conversation(conv["id"])
    assert c["subject_id"] == "R-1001" and c["verified_until"] is None


def test_unknown_tool_and_bad_args_return_errors_not_crashes(fake_model_rt):
    rt = fake_model_rt([{"tool": "delete_everything"}, {"tool": "search_policy", "input": {"wrong": 1}}, {"text": "ok"}])
    conv = rt.conversation_for("web", None)
    res = rt.handle_turn(conv["id"], "web", "hi")
    assert res.reply == "ok"


def test_model_exception_escalates_instead_of_failing(fake_model_rt):
    rt = fake_model_rt([])

    def boom(*a, **k):
        raise RuntimeError("model down")

    rt.s.model.create = boom
    conv = rt.conversation_for("voice", MARIA)
    res = rt.handle_turn(conv["id"], "voice", "hello")
    assert res.escalated and res.path == "error"


@pytest.mark.parametrize("text,kind", [
    ("4111 1111 1111 1111", "card_number"), ("4111-1111-1111-1111", "card_number"),
    ("my ssn is 123-45-6789", "ssn"), ("cvv is 123", "card_security_code")])
def test_redaction(text, kind):
    out, found = g.redact(text)
    assert kind in found and "1111 1111" not in out and "6789" not in out


def test_redaction_ignores_non_luhn_numbers():
    out, found = g.redact("my order number is 1234 5678 9012 3456")
    assert found == []


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes, that works.", True), ("sure, go ahead", True),
    ("no", False), ("yes but actually change the date", False), ("what are the dates?", False), ("wait", False)])
def test_affirmative(text, expected):
    assert g.is_affirmative(text) is expected


@pytest.mark.parametrize("text,channel,category", [
    ("STOP", "sms", "opt_out"), ("please stop calling me", "voice", "opt_out"),
    ("my lawyer will be in touch", "voice", "legal"), ("I already paid that", "sms", "dispute"),
    ("can I speak to a person", "web", "human_request"), ("I smell gas in the hallway", "voice", "emergency"),
    ("what's my balance", "voice", None)])
def test_hard_triggers(text, channel, category):
    t = g.hard_trigger(text, channel)
    assert (t.category if t else None) == category
