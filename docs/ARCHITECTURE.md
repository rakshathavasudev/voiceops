# Architecture and design decisions

## Components

| Component | Responsibility | Production equivalent |
|---|---|---|
| `legacy_pms` | Mock vendor property-management API: the system of record for residents, ledgers, leases, renewals, prospects and tours | Yardi, RealPage, Entrata, AppFolio |
| `integrations/pms_client.py` | The only path to the PMS. Auth, retries, breaker, validation, durable writes | same, plus per-vendor adapters |
| `store.py` | Agent-owned state: conversations, transcript, events, audit log, write outbox, consent, contact attempts, spans, turn metrics | Postgres (+ Redis for hot state) |
| `agent/runtime.py` | One `handle_turn` per utterance on any channel | stateless workers behind a queue |
| `agent/tools.py` | Tool schemas and handlers; every safety rule that touches data | same |
| `agent/guardrails.py` | Redaction, hard triggers, the confirmation classifier, policy validation | same |
| `campaigns.py` | Outbound target selection and the compliance gate | durable workflow engine (Temporal) for cadences |
| `server.py` | Twilio voice/SMS webhooks, email inbound, console API | API gateway + webhook workers |

## Decisions

**Guardrails live in code, around the model.** The prompt describes the rules so the model behaves well, but nothing that moves money or exposes tenant data depends on the model following them:

- Tools take no resident ID. A conversation binds to one subject through caller ID plus `verify_identity`, so even a successful prompt injection is limited to that verified person's allowed actions.
- Writes are two-phase. `prepare_*` validates against org policy and stores a pending confirmation with a read-back. `commit_confirmed_action` succeeds only if the pending action was created in an earlier turn, the user's latest message is affirmative (and contains no "wait / actually / no"), and the confirmation hasn't expired. A negative reply cancels the pending action before the model even runs.
- The output guard blocks the resident's balance or rent figures from reaching an unverified caller, failing closed if it cannot check.

**Hard triggers bypass the model.** Emergencies, opt-outs (including carrier keywords), legal language, billing disputes and requests for a human are matched deterministically. They are cheap, instant, auditable and never "talked out of" escalating.

**Idempotency keys are derived, not random.** `sha256(org, conversation, action, params)` means a model that calls commit twice, a retried HTTP request, a lost response or a replayed webhook all map to one write. The PMS rejects a reused key with a different body.

**Durable writes via an outbox.** Intent is persisted before the PMS call. If the PMS is down after retries, the write stays `pending`, the agent tells the user it's being processed (never "done"), and `drain_outbox` finishes it later. The audit log records both the queueing and the deferred commit.

**State is rebuilt from the store each turn.** History comes from the cross-channel transcript, so a call can continue over SMS. Earlier turns' tool transcripts are compacted into short events (`payment_plan_committed`, `guardrail_blocked`, and so on) in a `case_state` block instead of being replayed, which keeps context small. The static system prompt and tool list are identical across turns and orgs, so they are prompt-cached; volatile state goes in a second, uncached system block.

**Model choice and effort are configuration.** The default is `claude-opus-5-5` at `low` effort for voice latency, with server-side refusal fallbacks enabled. Swapping to `claude-sonnet-5-5` or raising effort is an env var, and the eval suite is how you decide: compare task success, p95 latency and cost per conversation across settings rather than guessing.

**A scripted model for the harness.** `ScriptedModel` implements the same interface with keyword rules. It's not meant to be smart: it makes everything around the model (tools, guardrails, integrations, state, channels) testable deterministically in CI, and lets the demo run with no keys. The same scenarios then run against Claude with `--provider claude --trials N`.

## Data model (agent store)

- `conversations`: one open case per subject, shared by every channel; verification window, failed attempts, pending confirmation, purpose (inbound, collections, renewal, lead follow-up, application follow-up), status (active, escalated, closed)
- `contact_index`: phone/email to open case, so inbound on any channel finds the case without a PMS round trip (and still works when the PMS is down)
- `messages`, `events`: transcript and compact action log
- `audit_log`: append-only; each row hashes the previous row's hash with its own content
- `pending_writes`: the durable outbox, keyed by idempotency key
- `consent`, `contact_attempts`: per-channel consent and every outbound decision with its reason
- `spans`, `turns`: traces and per-turn latency, tokens, cost, guardrail blocks, tool errors

## Known limits of the demo

- SQLite and in-process workers; no queue between webhook and agent, so a slow model call holds the Twilio request. In production, acknowledge the webhook fast and stream audio.
- Voice uses Twilio `<Gather input="speech">` (turn-based). Barge-in and sub-second latency need Media Streams or a realtime voice model.
- The console API is unauthenticated and meant for local demos only.
- Policy retrieval is keyword BM25 over one handbook; fine for dozens of sections, not thousands.
