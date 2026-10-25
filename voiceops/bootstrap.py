"""Wire the services together. Used by the API server, the eval runner and tests."""
from __future__ import annotations

import httpx

from voiceops.agent.llm import make_model
from voiceops.agent.runtime import AgentRuntime, Services
from voiceops.config import Settings, org_config
from voiceops.integrations.pms_client import PMSClient
from voiceops.messaging import Messenger
from voiceops.store import Store
from voiceops.tracing import Tracer


def build_runtime(settings: Settings, store: Store | None = None, pms_http: httpx.Client | None = None,
                  pms_base_url: str | None = None, model=None, fast_retries: bool = False) -> AgentRuntime:
    store = store or Store(settings.db_path)
    tracer = Tracer(store)
    pms = PMSClient(pms_base_url or settings.pms_base_url, settings.pms_client_id, settings.pms_client_secret,
                    store, tracer, http=pms_http,
                    **({"backoff_base": 0.0, "sleep": lambda s: None} if fast_retries else {}))
    services = Services(store=store, pms=pms, messenger=Messenger(store, settings, tracer), tracer=tracer,
                        org_id=settings.org_id, org=org_config(settings.org_id), model=model or make_model(settings), settings=settings)
    return AgentRuntime(services)
