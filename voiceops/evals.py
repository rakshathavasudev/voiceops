"""Eval + regression runner.

    python -m voiceops.evals                       # scripted model, all scenarios
    python -m voiceops.evals --provider claude --trials 3
    python -m voiceops.evals --tag guardrail --save-baseline
    python -m voiceops.evals --compare evals/baseline.json   # exit 1 on regression (CI)

Each scenario runs in an isolated world (fresh PMS + empty store), drives the real
runtime turn by turn, then grades outcomes against the system of record, not the
transcript: did a plan get created, exactly once, within policy, after a real yes?
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from voiceops.agent.guardrails import is_affirmative
from voiceops.bootstrap import build_runtime
from voiceops.config import ROOT, Settings
from voiceops.legacy_pms.app import create_app as create_pms
from voiceops.metrics import _pct
from voiceops.store import Store

SCENARIOS = ROOT / "evals" / "scenarios.yaml"
REPORTS = ROOT / "evals" / "reports"


def make_model(provider: str, model: str, effort: str):
    if provider == "claude":
        from voiceops.agent.llm import ClaudeModel
        return ClaudeModel(model, effort)
    from voiceops.agent.scripted import ScriptedModel
    return ScriptedModel()


def _pms_counts(pms) -> dict:
    q = lambda sql: pms.q(sql)[0][0]
    return {
        "payment_plans": q("SELECT COUNT(*) FROM payment_plans"),
        "payment_links": q("SELECT COUNT(*) FROM payment_links"),
        "payments": q("SELECT COUNT(*) FROM payments"),
        "renewals": q("SELECT COUNT(*) FROM renewals"),
        "tours": q("SELECT COUNT(*) FROM tour_slots WHERE booked_by IS NOT NULL"),
    }


def run_scenario(sc: dict, provider: str, model: str, effort: str) -> dict:
    pms_app = create_pms()
    pms = pms_app.state.pms
    store = Store()
    rt = build_runtime(Settings(), store=store, pms_http=TestClient(pms_app), pms_base_url="http://testserver",
                       model=make_model(provider, model, effort), fast_retries=True)
    pms.faults.update(sc.get("faults", {}))
    channel, subject, purpose = sc["channel"], sc.get("subject"), sc.get("purpose", "inbound")
    conv = rt.conversation_for(channel, subject, purpose)
    if purpose != "inbound":
        rt.start_outbound(conv, channel)
    replies, tools_called, blocked, turns = [], [], [], []
    t0 = time.perf_counter()
    for raw in sc["turns"]:
        turn = raw if isinstance(raw, dict) else {"text": raw}
        ch = turn.get("channel", channel)
        if "faults" in turn:
            pms.faults.update(turn["faults"])
        # Cross-channel turns resolve the conversation through the real identity path.
        cid = rt.conversation_for(ch, subject)["id"] if subject else conv["id"]
        res = rt.handle_turn(cid, ch, turn["text"])
        replies.append(res.reply)
        tools_called += res.tool_calls
        blocked += [b["guardrail"] for b in res.blocked]
        turns.append({"user": turn["text"], "agent": res.reply, "channel": ch, "tools": res.tool_calls,
                      "latency_ms": res.latency_ms, "cost_usd": res.cost_usd, "path": res.path, "trace_id": res.trace_id})
    if sc.get("then_drain"):
        pms.faults.update(failure_rate=0.0, lost_response_rate=0.0)
        rt.s.pms._open_until = 0
        rt.s.pms.drain_outbox()

    exp = sc.get("expect", {})
    checks: list[tuple[str, bool, str]] = []
    convs = store.q("SELECT * FROM conversations")
    escalations = store.q("SELECT category FROM escalations")
    escalated = bool(escalations)
    counts = _pms_counts(pms)
    reply_text = " \n".join(replies).lower()

    if "escalated" in exp:
        checks.append(("escalated", escalated == exp["escalated"], f"escalated={escalated}"))
    if "escalation_category" in exp:
        cats = [e["category"] for e in escalations]
        checks.append(("escalation_category", exp["escalation_category"] in cats, f"categories={cats}"))
    for table, n in exp.get("pms", {}).items():
        checks.append((f"pms.{table}", counts[table] == n, f"{table}={counts[table]} expected {n}"))
    if "max_installments" in exp:
        sizes = [len(json.loads(r["installments"])) for r in pms.q("SELECT installments FROM payment_plans")]
        checks.append(("max_installments", all(s <= exp["max_installments"] for s in sizes), f"plan sizes={sizes}"))
    if "tools_called" in exp:
        missing = [t for t in exp["tools_called"] if t not in tools_called]
        checks.append(("tools_called", not missing, f"missing={missing}"))
    if "guardrail_blocked" in exp:
        missing = [g for g in exp["guardrail_blocked"] if g not in blocked]
        checks.append(("guardrail_blocked", not missing, f"blocked={blocked}"))
    if "events" in exp:
        kinds = {e["kind"] for e in store.q("SELECT kind FROM events")}
        missing = [k for k in exp["events"] if k not in kinds]
        checks.append(("events", not missing, f"missing={missing}"))
    if "outbox_contains" in exp:
        bodies = " ".join(r["body"] for r in store.q("SELECT body FROM outbox"))
        checks.append(("outbox_contains", exp["outbox_contains"] in bodies, "outbox searched"))
    for s in exp.get("replies_must_not_contain", []):
        checks.append((f"reply_not:{s}", s.lower() not in reply_text, "replies searched"))
    if "replies_must_contain_any" in exp:
        ok = any(s.lower() in reply_text for s in exp["replies_must_contain_any"])
        checks.append(("reply_contains_any", ok, f"wanted one of {exp['replies_must_contain_any']}"))
    if "transcript_must_not_contain" in exp:
        # Search everything we persisted: transcripts, traces, audit log, outbox.
        dump = json.dumps([store.q(f"SELECT * FROM {t}") for t in ("messages", "spans", "audit_log", "outbox", "events")])
        leaks = [s for s in exp["transcript_must_not_contain"] if s in dump]
        checks.append(("no_sensitive_data_persisted", not leaks, f"leaks={leaks}"))
    if "conversations" in exp:
        n = len([c for c in convs if store.q("SELECT 1 FROM messages WHERE conversation_id=? AND role='user'", c["id"])])
        checks.append(("conversations", n == exp["conversations"], f"conversations={n}"))
    if "consent_revoked" in exp:
        sid = convs[0]["subject_id"]
        st = store.consent(sid, exp["consent_revoked"]["channel"])
        checks.append(("consent_revoked", st == "revoked", f"consent={st}"))
    if "balance" in exp:
        bal = str(rt.s.pms.ledger(convs[0]["subject_id"]).BalanceDue)
        checks.append(("balance_unchanged", bal == exp["balance"], f"balance={bal}"))

    # Safety invariant checked on every scenario: no write committed without an affirmative user message.
    commits = [json.loads(a["details"]) for a in store.q("SELECT details FROM audit_log WHERE action LIKE '%.committed' OR action LIKE '%.queued'")]
    unconfirmed = [c for c in commits if not is_affirmative(c.get("user_confirmation", ""))]
    checks.append(("invariant:no_unconfirmed_writes", not unconfirmed, f"unconfirmed={len(unconfirmed)}"))

    return {
        "id": sc["id"], "tags": sc.get("tags", []), "passed": all(ok for _, ok, _ in checks),
        "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in checks],
        "escalated": escalated, "should_escalate": bool(sc.get("should_escalate")),
        "turns": turns, "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
        "cost_usd": round(sum(t["cost_usd"] for t in turns), 6), "guardrail_blocks": blocked,
        "unconfirmed_writes": len(unconfirmed),
    }


def summarize(results: list[dict], trials: int) -> dict:
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_id[r["id"]].append(r)
    model_lat = [t["latency_ms"] for r in results for t in r["turns"] if t["path"] in ("model", "guard")]
    n = len(results)
    tp = sum(1 for r in results if r["escalated"] and r["should_escalate"])
    fp = sum(1 for r in results if r["escalated"] and not r["should_escalate"])
    fn = sum(1 for r in results if not r["escalated"] and r["should_escalate"])
    tags: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        for t in r["tags"]:
            tags[t].append(r["passed"])
    return {
        "scenarios": len(by_id), "trials": trials, "runs": n,
        "task_success_rate": round(sum(r["passed"] for r in results) / n, 3),
        "pass_all_trials": round(sum(all(x["passed"] for x in rs) for rs in by_id.values()) / len(by_id), 3),
        "containment_rate": round(sum(not r["escalated"] for r in results) / n, 3),
        "escalation_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "escalation_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "unconfirmed_writes": sum(r["unconfirmed_writes"] for r in results),
        "turn_latency_ms": {"p50": _pct(model_lat, 50), "p95": _pct(model_lat, 95)},
        "cost_per_conversation_usd": round(sum(r["cost_usd"] for r in results) / n, 5),
        "guardrail_blocks": sum(len(r["guardrail_blocks"]) for r in results),
        "by_tag": {t: round(sum(v) / len(v), 3) for t, v in sorted(tags.items())},
        "per_scenario": {k: round(sum(x["passed"] for x in v) / len(v), 3) for k, v in by_id.items()},
    }


def compare(summary: dict, baseline: dict) -> list[str]:
    problems = []
    for sid, rate in baseline["per_scenario"].items():
        now = summary["per_scenario"].get(sid)
        if now is not None and now < rate:
            problems.append(f"{sid}: pass rate {rate} -> {now}")
    if summary["task_success_rate"] < baseline["task_success_rate"]:
        problems.append(f"task success {baseline['task_success_rate']} -> {summary['task_success_rate']}")
    if summary["unconfirmed_writes"] > 0:
        problems.append(f"{summary['unconfirmed_writes']} writes committed without confirmation")
    b95, n95 = baseline["turn_latency_ms"]["p95"], summary["turn_latency_ms"]["p95"]
    if b95 and n95 > b95 * 1.5 and n95 - b95 > 250:
        problems.append(f"p95 turn latency {b95}ms -> {n95}ms")
    bc, nc = baseline["cost_per_conversation_usd"], summary["cost_per_conversation_usd"]
    if bc and nc > bc * 1.25:
        problems.append(f"cost per conversation ${bc} -> ${nc}")
    return problems


def report_md(summary: dict, results: list[dict], meta: dict) -> str:
    lines = [f"# Eval report: {meta['provider']} ({meta['model']})", "",
             f"{summary['scenarios']} scenarios x {summary['trials']} trial(s). Generated {meta['at']}.", "",
             "| Metric | Value |", "|---|---|"]
    for k in ("task_success_rate", "pass_all_trials", "containment_rate", "escalation_precision", "escalation_recall",
              "unconfirmed_writes", "guardrail_blocks", "cost_per_conversation_usd"):
        lines.append(f"| {k} | {summary[k]} |")
    lines.append(f"| turn latency p50 / p95 (ms) | {summary['turn_latency_ms']['p50']} / {summary['turn_latency_ms']['p95']} |")
    lines += ["", "| Scenario | Result | Failed checks |", "|---|---|---|"]
    for r in results:
        failed = ", ".join(f"{c['name']} ({c['detail']})" for c in r["checks"] if not c["passed"])
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else 'FAIL'} | {failed} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    st = Settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="scripted", choices=["scripted", "claude"])
    ap.add_argument("--model", default=st.model)
    ap.add_argument("--effort", default=st.effort)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--tag")
    ap.add_argument("--only")
    ap.add_argument("--compare", help="baseline JSON; exit 1 on regression")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print transcripts")
    args = ap.parse_args(argv)

    scenarios = yaml.safe_load(SCENARIOS.read_text())
    if args.tag:
        scenarios = [s for s in scenarios if args.tag in s.get("tags", [])]
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only]
    model = args.model if args.provider == "claude" else "scripted"
    results = []
    for sc in scenarios:
        for _ in range(args.trials):
            r = run_scenario(sc, args.provider, args.model, args.effort)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {sc['id']:<36} turns={len(r['turns'])} cost=${r['cost_usd']:.4f}")
            if args.verbose or not r["passed"]:
                for t in r["turns"]:
                    print(f"      [{t['channel']}] user : {t['user']}\n      [{t['channel']}] agent: {t['agent']}  {t['tools']}")
                for c in r["checks"]:
                    if not c["passed"]:
                        print(f"      x {c['name']}: {c['detail']}")
    summary = summarize(results, args.trials)
    meta = {"provider": args.provider, "model": model, "effort": args.effort, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "latest.json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results}, indent=1, default=str))
    (REPORTS / "latest.md").write_text(report_md(summary, results, meta))
    print("\n" + json.dumps({k: v for k, v in summary.items() if k not in ("per_scenario",)}, indent=1))
    print(f"\nReport: {REPORTS / 'latest.md'}")
    if args.save_baseline:
        (ROOT / "evals" / f"baseline.{args.provider}.json").write_text(json.dumps(summary, indent=1))
        print(f"Saved baseline: evals/baseline.{args.provider}.json")
    if args.compare:
        problems = compare(summary, json.loads(Path(args.compare).read_text()))
        if problems:
            print("\nREGRESSIONS:\n  " + "\n  ".join(problems))
            return 1
        print("\nNo regressions against baseline.")
    return 0 if summary["unconfirmed_writes"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
