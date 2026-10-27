"""Minimal tracer: nested spans for turns, model calls, tool calls and HTTP attempts.

Spans land in the store so the console can render a waterfall per turn. The shape
(trace_id / span_id / parent_id / attrs) maps 1:1 onto OpenTelemetry if you want to
export to Honeycomb, Datadog or Langfuse instead.
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)


def current_trace_id() -> str | None:
    return _trace_id.get()


class Span:
    def __init__(self, name: str, kind: str, attrs: dict) -> None:
        self.name, self.kind, self.attrs = name, kind, dict(attrs)
        self.status = "ok"

    def set(self, **attrs) -> None:
        self.attrs.update(attrs)

    def error(self, message: str) -> None:
        self.status = "error"
        self.attrs["error"] = message


class Tracer:
    def __init__(self, store) -> None:
        self.store = store

    @contextmanager
    def span(self, name: str, kind: str = "internal", new_trace: bool = False, **attrs):
        trace_id = _trace_id.get()
        if new_trace or trace_id is None:
            trace_id = "t-" + uuid.uuid4().hex[:12]
        span_id = uuid.uuid4().hex[:8]
        parent = None if new_trace else _span_id.get()
        t_tok, s_tok = _trace_id.set(trace_id), _span_id.set(span_id)
        span = Span(name, kind, attrs)
        start_wall = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            duration = (time.perf_counter() - start) * 1000
            _trace_id.reset(t_tok)
            _span_id.reset(s_tok)
            self.store.x(
                "INSERT INTO spans(trace_id, span_id, parent_id, name, kind, start_ts, duration_ms, status, attrs) VALUES (?,?,?,?,?,?,?,?,?)",
                trace_id, span_id, parent, name, kind, start_wall, round(duration, 2), span.status,
                json.dumps(span.attrs, default=str)[:8000],
            )

    def trace(self, trace_id: str) -> list[dict]:
        rows = self.store.q("SELECT * FROM spans WHERE trace_id=? ORDER BY start_ts, id", trace_id)
        return [{**r, "attrs": json.loads(r["attrs"])} for r in rows]
