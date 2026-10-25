import warnings

import pytest
from fastapi.testclient import TestClient

from voiceops.agent.scripted import ScriptedModel
from voiceops.bootstrap import build_runtime
from voiceops.config import Settings
from voiceops.legacy_pms.app import create_app
from voiceops.store import Store

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def pms_app():
    return create_app()


@pytest.fixture
def rt(pms_app):
    return build_runtime(Settings(), store=Store(), pms_http=TestClient(pms_app), pms_base_url="http://testserver",
                         model=ScriptedModel(), fast_retries=True)


class ScriptedCalls:
    """A 'model' that emits exactly the tool calls a test gives it: simulates a model that
    misbehaves (skips confirmation, oversteps policy) to prove the guardrails hold anyway."""

    model = "fake"

    def __init__(self, plan):
        self.plan = list(plan)

    def create(self, system, messages, tools):
        from voiceops.agent.llm import Block, ModelResponse, Usage
        step = self.plan.pop(0) if self.plan else {"text": "ok"}
        if "tool" in step:
            raw = [{"type": "tool_use", "id": f"toolu_{len(self.plan)}", "name": step["tool"], "input": step.get("input", {})}]
            blocks = [Block("tool_use", id=raw[0]["id"], name=step["tool"], input=step.get("input", {}))]
            return ModelResponse(raw, blocks, "tool_use", Usage(10, 5), "fake", 0)
        return ModelResponse([{"type": "text", "text": step["text"]}], [Block("text", text=step["text"])], "end_turn", Usage(10, 5), "fake", 0)


@pytest.fixture
def fake_model_rt(pms_app):
    def make(plan):
        return build_runtime(Settings(), store=Store(), pms_http=TestClient(pms_app), pms_base_url="http://testserver",
                             model=ScriptedCalls(plan), fast_retries=True)
    return make
