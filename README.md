# AHF Finance AI ChatBot

A production-grade, **read-only** AI assistant for AHF finance staff. It answers
routine accounts-payable, procurement, and finance questions — invoice/payment
status, PO and PR status, vendor onboarding status, T&E policy, approval
thresholds — grounds every policy answer in approved company documents, and
escalates to a human when it is not confident.

It performs **no write-back to SAP S/4HANA**: no bot-initiated approvals,
postings, releases, or payments, ever.

## Architecture

```
User prompt in SAP Joule
        │  A2A (JSON-RPC 2.0)  — BTP destination: AHF_FINANCE_AGENT_DEV
        ▼
┌──────────────────────────────┐   OData (GET only)   ┌────────────┐
│  AHF Finance ChatBot         │ ───────────────────▶ │  S/4HANA   │
│  A2A server (this repo)      │   dest: S43          └────────────┘
│  Cloud Foundry · us10-001    │
└───────────┬──────────────────┘
            │  dest: GENAICORE
            ▼
   SAP Generative AI Hub / AI Core   —  GPT 5.2 (answers) · text-embedding-3-small (indexing)
            │
            ▼
   HANA Cloud vector store   —  embedded knowledge base (approved finance/AP/procurement SOPs)
```

- **A2A server** — Python, `a2a-sdk[http-server]`, Starlette + uvicorn.
- **Joule** connects as an A2A client via a Joule Studio capability, reaching
  this agent through the `AHF_FINANCE_AGENT_DEV` BTP destination.
- Three BTP destinations, all resolved at runtime through the bound
  `destination-service` instance: `AHF_FINANCE_AGENT_DEV` (Joule → agent),
  `GENAICORE` (agent → AI Core), `S43` (agent → S/4HANA).

## Build status

| Step | Scope | Status |
|---|---|---|
| 1 | A2A server scaffold: structure, deps, health check, agent card, task lifecycle, sync request handling | ✅ done |
| 2 | GPT 5.2 via SAP Generative AI Hub (`GENAICORE` destination) | ✅ code done — live call needs a bound `destination` service / `DESTINATION_SERVICE_KEY` |
| 3 | S/4HANA read-only tools (5 OData services) | ✅ code done — GPT 5.2 tool-calling loop over `S43`; live call needs the bound `destination` service. Smoke: `GET /diag/s4` |
| 4 | RAG pipeline + HANA Cloud vector store | ⬜ |
| 5 | Async webhook path (60s A2A ceiling) | partial — `pushNotifications` wired |
| 6 | Escalation confidence bar | ⬜ |
| 7 | Cloud Foundry deploy | ⬜ |
| 8 | Joule integration | capability authored in `joule-capability/`; not deployed |
| 9 | Observability | partial — per-turn interaction records emitted |
| 10 | Tests | ongoing |

Since step 3, the agent answers specific invoice / payment / PO / PR / vendor
questions from live read-only S/4HANA lookups. Until step 4 lands it still
replies plainly that it is not yet grounded on **policy** documents and offers
escalation — a deliberate interim behaviour.

## Project layout

```
src/ahf_finance_agent/
├── __main__.py         # server entry point (uvicorn + Starlette); `python -m ahf_finance_agent`
├── server.py           # build_app(): A2A Starlette app + /health + /ready + /diag/llm + /diag/s4
├── agent_card.py       # Agent Card — what Joule discovers at /.well-known/agent.json
├── agent_executor.py   # A2A protocol bridge + task lifecycle
├── answering.py        # AnswerGenerator: question → GPT 5.2 (+ S/4HANA tool loop) → scrubbed answer
├── llm.py              # GenAIHubClient: GPT 5.2 via GENAICORE (openai SDK, tool calling)
├── s4hana.py           # S4HANAClient: read-only OData v2 GETs via the S43 destination
├── tools.py            # OpenAI tool schemas + dispatch for the 6 S/4HANA lookups
├── prompts.py          # staged system prompt (step 3: tools live, policy KB not yet)
├── guardrails.py       # scrub_response() / strip_sensitive_keys() — no PII / bank data out
├── btp/destinations.py # BTP destination resolution (+ CSRF-fallback, on-prem proxy)
├── task_store.py       # SQLite-backed A2A task store (threads survive `cf push`)
├── config.py           # env-driven settings, fail-fast
├── logging_setup.py    # JSON logs + PII redaction filter
├── escalation.py       # human-handoff wording (confidence bar lands in step 6)
└── observability.py    # one structured record per answered turn
tests/                  # pytest — no BTP creds or network needed
joule-capability/       # SAP Joule BYOA capability (schema 3.28.0) — deployed in step 8
scripts/create-destination.sh   # creates/updates the AHF_FINANCE_AGENT_DEV destination
manifest.yml, Procfile, runtime.txt, requirements.txt   # Cloud Foundry deploy
```

## Local development

```bash
uv sync
cp .env.example .env          # edit as needed; fine to leave defaults for step 1
uv run python -m ahf_finance_agent
```

```bash
# Agent card
curl -s localhost:8080/.well-known/agent.json | jq .

# Health / readiness
curl -s localhost:8080/health
curl -s localhost:8080/ready | jq .

# Downstream smoke tests (non-prod only): resolve the destination + one live call
curl -s localhost:8080/diag/llm | jq .
curl -s localhost:8080/diag/s4  | jq .   # one-row OData GET against S43, no business data

# A2A message/send round trip
curl -s localhost:8080/ -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":"1","method":"message/send",
  "params":{"message":{"role":"user","messageId":"m1",
    "parts":[{"kind":"text","text":"What is the status of invoice 5105601234?"}]}}}' | jq .
```

## Tests

```bash
uv run pytest
```

Unit tests cover the agent card, config, and the executor's task lifecycle; an
integration test drives a full A2A `message/send` round trip through the real
Starlette app (mocked at no external boundary — step 1 has none).

## Deployment

Buildpack notes (all learned from real failed deploys on this stack):

- `requirements.txt` is generated from the lock file and its **first line is a
  bare `.`** (plain, non-editable install). Regenerate after changing deps:
  `uv export --no-hashes --no-dev -o requirements.txt`.
- `runtime.txt` pins `python-3.13.x` — the buildpack otherwise defaults older.
- `PORT` is **not** set in `manifest.yml`; Cloud Foundry injects it.
- Route domain is `cfapps.us10-001.hana.ondemand.com` (confirmed via
  `cf domains` on this org — not assumed from the region name).

```bash
cf login -a https://api.cf.us10-001.hana.ondemand.com
cf target -o <org> -s <space>
cf push
curl -s https://finance-ai-chatbot.cfapps.us10-001.hana.ondemand.com/.well-known/agent.json
```

## Security & governance

- **Read-only, structurally.** Every S/4HANA tool issues `GET` only (step 3).
- **No PII / vendor banking data** in responses: pruned OData `$select` lists,
  key stripping, and an outbound text scrub; the model is also instructed never
  to request or repeat such data.
- Access follows existing S/4HANA roles via the agent's own service
  credentials. The Joule↔agent hop uses `NoAuthentication` **only** as a
  tracked dev gap; production requires an IAS App2App trust set up by an SAP
  admin (step 8).
