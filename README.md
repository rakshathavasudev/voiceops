# VoiceOps: AI agents that complete property-management workflows

VoiceOps is a working voice and messaging agent for a property manager. It handles resident and prospect conversations over **phone, SMS, web chat and email**, and completes real operational workflows against a **legacy system of record**: collections and payment plans, payment links, lease renewals, tour booking and lead follow-up.

The interesting part is everything around the model:

- **Guardrails in code, not in the prompt.** Identity verification, two-step confirmation that checks the user's own words, policy limits, idempotency, scope limits and human escalation.
- **Legacy integration that survives a bad day.** OAuth refresh, timeouts, retries with backoff, a circuit breaker, typed validation of a messy vendor API, and durable writes through an outbox.
- **One conversation across channels.** A call can continue over SMS without verifying again.
- **Compliant outbound.** Consent, quiet hours in the resident's time zone, frequency caps and dispute holds.
- **Evals, tracing and metrics.** 17 graded scenarios, task success, containment, escalation precision/recall, latency, cost per conversation, and a regression gate in CI.

![VoiceOps console](docs/console.png)

It runs fully offline with no API keys. A deterministic scripted model stands in for the LLM, and the legacy PMS is mocked, so you can demo it anywhere. Set `ANTHROPIC_API_KEY` to run the same agent on Claude, and add Twilio credentials to take real phone calls.

> The original single-file insurance call simulator (v1) is still here: [`insurance-call-simulator.html`](insurance-call-simulator.html).

---

## Quickstart

```bash
pip install -r requirements.txt
python -m voiceops            # console at http://localhost:8000 (mock PMS on :8001)
```

Open the console in Chrome, pick **Maria Lopez · Unit 4B**, press **Start conversation**, click the mic and say *"This is Maria Lopez in unit 4B. Can I split my balance into three payments?"*

| Run with | How |
|---|---|
| Offline (default) | nothing to configure; the scripted model drives the same tools and guardrails |
| Claude | `export ANTHROPIC_API_KEY=...` (optional: `VOICEOPS_MODEL=claude-sonnet-5-5`, `VOICEOPS_EFFORT=low\|medium`) |
| Real phone + SMS | Point a Twilio number's voice webhook at `POST {PUBLIC_BASE_URL}/twilio/voice` and SMS at `/twilio/sms`; set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_BASE_URL` (e.g. an ngrok URL). Signatures are validated when the auth token is set. |
| Frozen clock for demos | `VOICEOPS_NOW=2026-10-27T10:00:00-07:00` (quiet hours and dates become reproducible) |

```bash
python -m pytest -q                                   # 39 unit tests
python -m voiceops.evals                              # 17 eval scenarios, offline
python -m voiceops.evals --provider claude --trials 3 # the real agent, pass^k over 3 trials
python -m voiceops.evals --compare evals/baseline.scripted.json   # regression gate (CI)
```

The interview walkthrough is in **[docs/DEMO.md](docs/DEMO.md)**. Design notes are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## How a turn works

```mermaid
flowchart LR
  subgraph Channels
    V[Voice: browser mic / Twilio] --> R
    S[SMS: Twilio / simulator] --> R
    W[Web chat] --> R
    E[Email inbound] --> R
  end
  R[Resolve contact to open case<br/>shared across channels] --> P[Redact card numbers / SSNs]
  P --> H{Hard triggers<br/>emergency, opt-out, legal,<br/>dispute, human request}
  H -- yes --> X[Escalate / reply<br/>no model call]
  H -- no --> L[Model + tools loop<br/>budgets, traced]
  L --> T[Tool layer<br/>verification, scope, policy,<br/>confirmation check]
  T --> C[PMS client<br/>auth, retries, breaker,<br/>validation, outbox]
  C --> PMS[(Legacy PMS<br/>system of record)]
  T --> A[(Audit log<br/>hash-chained)]
  L --> G[Output guard<br/>no account data before verification]
  G --> O[Reply on the same channel]
```

## What maps to what

| What the role needs | Where it lives |
|---|---|
| Agents that complete workflows and write to a system of record with an audit trail | [`agent/tools.py`](voiceops/agent/tools.py) (prepare/commit writes), [`store.py`](voiceops/store.py) (hash-chained audit log) |
| Shared state across voice, SMS, web chat and email | [`agent/runtime.py`](voiceops/agent/runtime.py) `conversation_for` + contact index, history rebuilt per turn in [`agent/prompts.py`](voiceops/agent/prompts.py) |
| Compliant outbound for collections, abandoned applications, leads and renewals | [`campaigns.py`](voiceops/campaigns.py) |
| Guardrails for payments and tenant records (confirmation, idempotency, scope, escalation) | [`agent/guardrails.py`](voiceops/agent/guardrails.py), [`agent/tools.py`](voiceops/agent/tools.py), tests in [`tests/test_guardrails.py`](tests/test_guardrails.py) |
| Legacy API integration (auth, retries, timeouts, validation, durable writes) | [`integrations/pms_client.py`](voiceops/integrations/pms_client.py) against [`legacy_pms/app.py`](voiceops/legacy_pms/app.py) |
| Evaluation sets, regression runs, tracing, operational metrics | [`evals/scenarios.yaml`](evals/scenarios.yaml), [`evals.py`](voiceops/evals.py), [`tracing.py`](voiceops/tracing.py), [`metrics.py`](voiceops/metrics.py), [CI](.github/workflows/ci.yml) |
| Tool calling, RAG, structured outputs, context management | strict JSON-schema tools, [`agent/knowledge.py`](voiceops/agent/knowledge.py) (policy retrieval), prompt caching + compacted history in [`agent/prompts.py`](voiceops/agent/prompts.py) |
| Telephony, messaging providers, PCI-scoped payments, consent | [`integrations/twilio.py`](voiceops/integrations/twilio.py), payment links instead of card capture, consent table and STOP handling |
| Tenant-specific configuration | [`config/orgs/harborview.json`](config/orgs/harborview.json) and [`config/knowledge/harborview.md`](config/knowledge/harborview.md) |

## Project layout

```
voiceops/
  agent/          runtime loop, tools, guardrails, prompts, retrieval, Claude + scripted models
  integrations/   legacy PMS client, Twilio helpers
  legacy_pms/     mock vendor API with fault injection (the system of record)
  campaigns.py    outbound workflows + compliance gate
  store.py        agent-side state: conversations, audit log, outbox, consent, traces
  server.py       channel webhooks + console API
  evals.py        eval runner and regression gate
config/           per-org policy and handbook
evals/            scenarios, baseline, reports
web/console.html  operator console (voice via Web Speech API)
tests/            unit tests, including a misbehaving-model suite
```

## Security notes

- The agent never handles card data. Payments go through a secure link; card numbers, CVVs and SSNs are redacted before text reaches the model, logs, traces or storage (an eval checks every table).
- Caller ID and sender address identify a person but never verify them. Account tools require `verify_identity`, and an output guard blocks account figures in replies to unverified callers.
- Twilio webhooks are signature-checked when `TWILIO_AUTH_TOKEN` is set. The console API has no auth: it is a local demo tool and must not be exposed publicly as-is.
