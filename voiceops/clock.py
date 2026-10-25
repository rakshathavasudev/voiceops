"""Injectable clock so quiet hours, verification windows and campaigns are testable.

Set VOICEOPS_NOW=2026-10-27T10:00:00-07:00 to freeze time for a live demo.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_frozen: datetime | None = None


def freeze(at: datetime | None) -> None:
    global _frozen
    _frozen = at


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    env = os.environ.get("VOICEOPS_NOW")
    if env:
        return datetime.fromisoformat(env)
    return datetime.now(timezone.utc)
