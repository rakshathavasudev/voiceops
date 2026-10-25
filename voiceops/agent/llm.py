"""Model adapters. The runtime talks to a small interface so the same loop can run on
Claude or on the deterministic scripted policy used for offline demos and CI."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

# USD per million tokens: (input, output, cache_read). Cache writes bill at 1.25x input.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "scripted": (0.0, 0.0, 0.0),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost(self, model: str) -> float:
        inp, out, cached = PRICING.get(model, PRICING["claude-opus-5-5"])
        return (self.input_tokens * inp + self.output_tokens * out + self.cache_read_tokens * cached
                + self.cache_write_tokens * inp * 1.25) / 1_000_000


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class ModelResponse:
    raw_content: list[Any]  # what gets appended to history unchanged (keeps thinking blocks intact)
    blocks: list[Block]
    stop_reason: str
    usage: Usage
    model: str
    latency_ms: float


class ClaudeModel:
    name = "claude"

    def __init__(self, model: str, effort: str = "low", client: Any = None) -> None:
        self.model, self.effort = model, effort
        self.client = client or anthropic.Anthropic(max_retries=2, timeout=30.0)
        self.use_fallbacks = os.environ.get("VOICEOPS_FALLBACKS", "1") == "1"

    def create(self, system: list[dict], messages: list[dict], tools: list[dict]) -> ModelResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=4096, system=system, tools=tools, messages=messages,
            thinking={"type": "adaptive"}, output_config={"effort": self.effort},
        )
        if self.use_fallbacks:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        t0 = time.perf_counter()
        resp = self.client.beta.messages.create(**kwargs)
        latency = (time.perf_counter() - t0) * 1000
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append(Block("text", text=b.text))
            elif b.type == "tool_use":
                blocks.append(Block("tool_use", id=b.id, name=b.name, input=dict(b.input)))
        u = resp.usage
        usage = Usage(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
                      getattr(u, "cache_creation_input_tokens", 0) or 0)
        return ModelResponse(list(resp.content), blocks, resp.stop_reason or "end_turn", usage, resp.model or self.model, latency)


def make_model(settings) -> Any:
    if settings.model_provider == "claude":
        return ClaudeModel(settings.model, settings.effort)
    from voiceops.agent.scripted import ScriptedModel
    return ScriptedModel()
