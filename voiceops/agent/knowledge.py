"""Tiny retrieval over the org's policy handbook (one markdown section per chunk).

BM25-style keyword scoring is plenty for a few dozen policy sections and keeps the
demo dependency-free; swap in embeddings + pgvector when the corpus grows.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

from voiceops.config import knowledge_base

_STOP = set("a an the and or of to in on for is are be can i my me do does how what when where with your you it at by".split())


def _tokens(text: str) -> list[str]:
    toks = []
    for t in re.findall(r"[a-z0-9$]+", text.lower()):
        if t in _STOP:
            continue
        toks.append(t[:-1] if t.endswith("s") and len(t) > 3 else t)  # crude plural folding
    return toks


@lru_cache(maxsize=None)
def _chunks(org_id: str) -> list[tuple[str, str, Counter]]:
    out = []
    for part in re.split(r"^## ", knowledge_base(org_id), flags=re.M)[1:]:
        title, _, body = part.partition("\n")
        out.append((title.strip(), body.strip(), Counter(_tokens(title + " " + title + " " + body))))
    return out


def search(org_id: str, query: str, k: int = 2) -> list[dict]:
    chunks = _chunks(org_id)
    q = _tokens(query)
    n = len(chunks)
    avg = sum(sum(c.values()) for _, _, c in chunks) / max(n, 1)
    scored = []
    for title, body, tf in chunks:
        length = sum(tf.values())
        score = 0.0
        for term in q:
            df = sum(1 for _, _, c in chunks if term in c)
            if not df or term not in tf:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * tf[term] * 2.2 / (tf[term] + 1.2 * (0.25 + 0.75 * length / avg))
        if score > 0:
            scored.append((score, title, body))
    scored.sort(reverse=True)
    return [{"section": t, "text": b, "score": round(s, 2)} for s, t, b in scored[:k]]
