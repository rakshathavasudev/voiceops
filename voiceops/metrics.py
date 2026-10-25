"""Operational metrics computed from the store: the numbers you'd put on a dashboard
and alert on once agents are in production."""
from __future__ import annotations

import json
from collections import Counter


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return round(values[k], 1)


def compute(store) -> dict:
    convs = store.q("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id AND m.role='user') AS user_msgs FROM conversations c")
    engaged = [c for c in convs if c["user_msgs"] > 0]
    escalated = [c for c in engaged if c["status"] == "escalated"]
    turns = store.q("SELECT * FROM turns")
    model_turns = [t for t in turns if t["llm_calls"]]
    lat = [t["latency_ms"] for t in model_turns]
    audit = store.q("SELECT action, outcome, details FROM audit_log")
    writes = Counter(a["action"] for a in audit if a["action"].endswith((".committed", ".queued")))
    replays = sum(1 for a in audit if a["action"].endswith(".committed") and json.loads(a["details"]).get("replayed"))
    tool_spans = store.q("SELECT name, status FROM spans WHERE kind='tool'")
    http = store.q("SELECT status, attrs FROM spans WHERE kind='http'")
    retries = sum(1 for h in http if json.loads(h["attrs"]).get("attempt", 1) > 1)
    blocks = Counter(json.loads(e["data"]).get("guardrail") for e in store.q("SELECT data FROM events WHERE kind='guardrail_blocked'"))
    esc = Counter(e["category"] for e in store.q("SELECT category FROM escalations"))
    tool_counts = Counter(t["name"].removeprefix("tool ") for t in tool_spans)
    tool_err = Counter(t["name"].removeprefix("tool ") for t in tool_spans if t["status"] == "error")
    cost = sum(t["cost_usd"] for t in turns)
    pending = store.q("SELECT COUNT(*) n FROM pending_writes WHERE status='pending'")[0]["n"]
    return {
        "conversations": len(engaged),
        "containment_rate": round(1 - len(escalated) / len(engaged), 3) if engaged else None,
        "escalation_rate": round(len(escalated) / len(engaged), 3) if engaged else None,
        "escalations_by_category": dict(esc),
        "workflows_completed": {k.rsplit(".", 1)[0]: v for k, v in writes.items() if k.endswith(".committed")},
        "writes_queued": sum(v for k, v in writes.items() if k.endswith(".queued")),
        "writes_pending_now": pending,
        "idempotent_replays": replays,
        "turns": len(turns),
        "turns_handled_without_model": len(turns) - len(model_turns),
        "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": _pct(lat, 100)},
        "cost_usd": {"total": round(cost, 4), "per_conversation": round(cost / len(engaged), 4) if engaged else 0.0,
                     "per_turn": round(cost / len(turns), 5) if turns else 0.0},
        "tokens": {"input": sum(t["input_tokens"] for t in turns), "output": sum(t["output_tokens"] for t in turns),
                   "cache_read": sum(t["cache_read_tokens"] for t in turns)},
        "tool_calls": dict(tool_counts),
        "tool_error_rate": round(sum(tool_err.values()) / len(tool_spans), 3) if tool_spans else 0.0,
        "guardrail_blocks": dict(blocks),
        "pms_http": {"requests": len(http), "errors": sum(1 for h in http if h["status"] == "error"), "retries": retries},
    }
