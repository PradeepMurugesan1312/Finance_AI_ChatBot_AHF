# CLAUDE.md — AHF Finance AI ChatBot

Read this first. It is the fast path to being productive in this repo.

## What this is

A **read-only** AI assistant for AHF finance staff (accounts payable, procurement,
finance). It answers "status of invoice / payment / PO / PR / vendor" from **live
S/4HANA OData** and "what's the policy / threshold / how do I" from a **RAG policy
knowledge base**, and hands off to a human when it isn't grounded.

It performs **no write-back to SAP, ever** — every S/4HANA call is a `GET`, by
construction. This is a hard product constraint, not a config toggle.

Runs as an **A2A server** (JSON-RPC 2.0) that **SAP Joule** calls as an A2A client.

```
Joule (webclient)  ──A2A──▶  this agent (Cloud Foundry, us10-001)
                                  │  dest GENAICORE ──▶ SAP Generative AI Hub / AI Core (GPT 5.2)
                                  │  dest S43 (OData GET only) ──▶ S/4HANA (on-prem, via Cloud Connector)
                                  └  policy vector index (local JSON default; HANA Cloud stub)
```

## Repo / branch topology (as of this session, 2026-09)

- Remote: `github.com/PradeepMurugesan1312/Finance_AI_ChatBot_AHF`.
- `main` = `origin/main` = `a617fad` "deploy bug fix" — **predates the S/4HANA
  lookup tools entirely** (Step 3 is not on main).
- `testing` (and `step-3-s4hana-tools`) = `4bd50fc` — Step 3 base: 6 S/4HANA tools.
- **The real work is an uncommitted working tree on `testing`** (~1400 lines in
  `s4hana.py` + `tests/`, plus untracked `domains.py`, `knowledge_base.py`,
  `kb_ingest.py`, `docs/`, `knowledge_base/`). Nothing is committed or merged.
- A coworker deployed a build to Joule from **their own clone** — not visible
  here. When you change code, it must land wherever that deploy tracks.

## Build status (README "Build status" table is authoritative)

| Step | Scope | State |
|---|---|---|
| 1–2 | A2A scaffold; GPT 5.2 via GENAICORE | done |
| 3 | S/4HANA read-only procure-to-pay OData tools | code done, live-verified on S43 |
| 4 | RAG pipeline + `search_policy_docs` + local vector index | code done |
| 5 | Async webhook / pushNotifications | partial |
| 6 | Escalation confidence bar | not started |
| 7 | Cloud Foundry deploy | **not marked done** |
| 8 | Joule integration | capability authored in `joule-capability/`, "not deployed" per README (a coworker has since deployed one) |

## Module map (`src/ahf_finance_agent/`)

- `__main__.py` / `server.py` — Starlette app; `/health`, `/ready`, and diag
  routes: `/diag/llm`, `/diag/s4`, `/diag/s4/samples`, `/diag/s4/catalog`,
  `/diag/kb`, `/diag/domains`.
- `agent_executor.py` — A2A protocol bridge + task lifecycle.
- `answering.py` — `AnswerGenerator`: question → GPT 5.2 tool-calling loop →
  scrubbed answer. Tool iteration budget = `S4HANA_MAX_TOOL_ITERATIONS` (4).
- `llm.py` — `GenAIHubClient`: GPT 5.2 via GENAICORE (openai SDK, tool calling).
- `s4hana.py` — `S4HANAClient`: read-only OData v2 GETs via the S43 destination.
  **See "S/4HANA client" below — this is where the complexity lives.**
- `tools.py` — OpenAI tool schemas + dispatch. **14 S/4HANA tools** +
  `search_policy_docs`. `dispatch_tool` never raises; failures come back as
  `{"error": …}` so the model can hand off.
- `knowledge_base.py` / `kb_ingest.py` — RAG: chunk, embed (AI Core
  `text-embedding-3-small` if `EMBEDDING_DEPLOYMENT_ID` set, else local fallback),
  local JSON index (default) or HANA vector store stub (`KB_BACKEND=hana`).
- `domains.py` — finance domain registry, `status` ∈ `live | kb_only | planned`,
  served at `/diag/domains`. Live: AP, Procurement (PO/PR), Goods Receipt,
  3-way match, Vendor/BP, Payments & Clearing. kb_only: GL/journal entries,
  G/L balances, AR, cost centers, profit centers, budgeting, fixed assets,
  bank & cash, tax.
- `prompts.py` — staged system prompt. `RAG_SYSTEM_PROMPT` is current (Step 4);
  `TOOLS_SYSTEM_PROMPT` (Step 3) and `INTERIM_SYSTEM_PROMPT` (Step 2) kept for
  rollback. The Step-2 prompt says "NOT connected to live SAP" — if the deployed
  bot says that, it is running the wrong prompt stage.
- `guardrails.py` — `scrub_response()` / `strip_sensitive_keys()`: no PII / bank /
  tax / IBAN / SWIFT data leaves the process. Belt-and-braces with pruned OData
  `$select` lists.
- `btp/destinations.py` — BTP destination resolution (+ on-prem proxy, CSRF).
- `task_store.py` — SQLite A2A task store (threads survive `cf push`).

## The 14 S/4HANA tools

`get_invoice_status`, `get_invoice_items`, `search_invoices_by_vendor`,
`get_payment_clearing_status`, `get_invoice_payment_status`,
`get_purchase_order_status`, `get_purchase_order_items`,
`get_purchase_order_delivery_schedule`, `get_purchase_order_approval_status`,
`check_three_way_match`, `get_goods_receipts_for_po`,
`get_purchase_requisition_status`, `get_vendor_details`, `get_budget_status`.

Notes:
- Invoice number is unique on its own — tools take just the number; fiscal year
  is optional and only passed if the user volunteers it. The model chains
  `get_invoice_status` → `get_invoice_items` / `check_three_way_match` itself.
- `get_invoice_payment_status` resolves the FI accounting document for you
  (header field → journal-entry reference/AWKEY → assume == invoice number, in
  that order; `accountingDocumentSource` says which).
- `check_three_way_match` is **computed by the agent** from invoice items + PO
  items + goods receipts; `paymentBlockingReason` is SAP's own signal. There is
  no released API for SAP's stored match result or block-release history.
- `get_budget_status` is **deliberately not connected** — no reliable budget
  OData surface across builds. Returns `budgetAvailable=false` + a report
  pointer (FMAVCR01 / S_ALR_87013019 / Cost Centers – Plan/Actual). Never
  estimates a figure. Always `grounded=false`.
- The F110 payment-run ID (LAUFD/LAUFI) is in **no** released API. Payment
  document + date + house bank identify the run; say AP Payments can pin it.

## S/4HANA client (`s4hana.py`) — how it's built

- **Structurally read-only**: only `_get()` exists. No post/patch/delete path.
- **Service catalogue** (`_SERVICE_CATALOG`): each logical capability maps to an
  ordered list of `(service, entity_set)` candidates. `resolve_capability()`
  probes them against the live tenant and locks onto the first that answers
  (cached 1h per client instance). `probe_catalog()` backs `/diag/s4/catalog`.
- **`_capability_query` / `_capability_entity`**: run the query through the
  catalogue and, if the resolved candidate turns out unusable here, advance to
  the next candidate (`_should_try_next_candidate`).
- **`_select_get()`**: self-healing `$select`. If Gateway rejects a field, drop
  it and retry, so a release-specific field gap degrades to a smaller
  projection instead of failing the whole lookup.
- **Error taxonomy** (`_get`): a genuine "no such record" 404 (OData error body)
  → `None` / `[]`. A bare 404 (ICF / inactive service, HTML body) → `S4HANAError`.
  401 → whole-system auth. 403 → user not authorised for that service. Unknown
  field/segment → `_UnknownODataSegment` (see below).
- `_lit()` rejects any identifier that isn't short + alphanumeric — keeps
  `$filter` injection off the table.

## S43 tenant quirks (the on-prem POC landscape) — memorise these

- On-prem S/4HANA, **company code 1710, fiscal year 2017**, host `192.168.8.69`,
  reached via Cloud Connector / on-prem proxy through destination `S43`.
- Live-verified IDs: supplier invoices **5100000016** and **5100000017**
  (FY 2017, CC 1710); business partner **1000000**.
- **Abbreviated entity-set names**: invoice items are `A_SuplrInvcItemPurOrdRef`
  (nav `to_SuplrInvcItemPurOrdRef`), NOT the un-abbreviated
  `A_SupplierInvoiceItemPurOrdReference` (Gateway 404s that as an unknown
  segment).
- **Dropped `$select` fields**: several standard fields are absent from this
  build's services (e.g. `IsPaid`); `_select_get()` drops them on retry.
- **`API_JOURNALENTRYITEMBASIC_SRV` has no `AccountingDocument`** on
  `A_JournalEntryItemBasic` on this build — see the bug below. This is an
  ~1809+ compositional API on a 1710 system; older FI line-item surface is
  `API_OPLACCTGDOCITEMCUBE_SRV` (catalogue candidate #2 for `journal_entry_item`).
- Composite-key GETs (`A_SupplierInvoice(SupplierInvoice='..',FiscalYear='..')`)
  404 on some builds while the collection `$filter` query returns the row —
  `get_invoice_status` uses `$filter`.

## Bug fixed this session (uncommitted, in `s4hana.py` + `tests/test_s4hana.py`)

**Symptom** (seen in Joule "API Testing" screenshots): "Has payment been sent for
invoice 5100000016?" → `400: Property 'AccountingDocument' not found in type
'…A_JournalEntryItemBasicType'`. Broke `get_payment_clearing_status`,
`get_invoice_payment_status`, the FI-doc reference resolver, the payment-run
summary. Vendor / budget / invoice-header / PO / GR / 3-way-match unaffected.

**Root cause**: the S43 build's journal-entry entity type lacks
`AccountingDocument`. Gateway signals this as a **400 "Property … not found in
type …"**, but the code only recognised the **404 "Resource not found for the
segment 'X'"** form, so the 400 dead-ended instead of failing over to the next
catalogue candidate.

**Fix**:
1. `_UNKNOWN_PROPERTY_RE` + `_unknown_field()` — recognise the 400 shape.
2. `_get()` raises `_UnknownODataSegment` for that 400 too → `_select_get()`
   drops the field, `_probe()` classifies the candidate `absent`.
3. `_should_try_next_candidate()` also advances when `_select_get()` exhausts
   retries on a needed `$filter` key (message contains `has no resource/segment`)
   → `journal_entry_item` fails over to `API_OPLACCTGDOCITEMCUBE_SRV`.

Result: the clearing lookups fail over to a service that models
`AccountingDocument`, or return a clean "not available on this system" the model
relays + hands off — no more raw 400.

**Still to verify on the live tenant** (needs someone with S43 access):
- `GET /diag/s4/catalog` — does any `journal_entry_item` candidate actually
  resolve on S43? If all three are `absent`, the tenant needs the correct
  service name added to `_SERVICE_CATALOG` (from Basis/FI), or the API activated
  in `/IWFND/MAINT_SERVICE` + the communication arrangement.
- `GET /diag/s4/samples` — real IDs for regression tests.
- Redeploy this branch (not `main`), re-run the screenshot questions.

**Known wart** (not fixed): if the bare `$top=1` probe succeeds but the filtered
query fails over, `/diag/s4/catalog` still shows candidate #1 as resolved. The
real signal is whether `get_payment_clearing_status` returns data.

## Prompt / model behaviour notes

- On a tool error the model should relay it plainly + hand off to AP Payments
  (as it does for `get_invoice_payment_status`). It sometimes instead degrades
  to a generic "I don't have access to those APIs" — worth a prompt nudge if it
  recurs after the fix.
- Guardrails that never relax: read-only; no PII/bank/tax data in or out;
  finance-only scope; hand off rather than guess.

## Dev / test

- **No `uv` on this box.** Use the project venv: `source .venv/bin/activate`.
- `python -m pytest -q` — full suite (156 passing after this session's fix).
- `python -m ahf_finance_agent.kb_ingest` — build the policy vector index.
- `python -m ahf_finance_agent` — run the server (also builds the index on boot
  if missing).
- `tests/test_s4hana.py` uses `_FakeClient` (monkeypatches `s4.httpx.Client` +
  `s4.resolve_destination`): routes GET by URL substring to canned bodies.
  `reject_field` (+ optional `reject_status`, `reject_message`) simulates a
  Gateway field rejection on `$select` or `$filter`. `_d(...)` wraps rows in
  `{"d": {"results": [...]}}`.
- `tests/test_answering.py` has a scripted fake-LLM harness (`tool_call_result`
  in `tests/helpers.py`) — use it for end-to-end tool-selection regression tests
  without a live LLM or S/4HANA.

## Deploy

- `manifest.yml`: CF app `finance-ai-chatbot`, route
  `finance-ai-chatbot.cfapps.us10-001.hana.ondemand.com`, buildpack python,
  bound services `destination-service` + `btp-connectivity` (S43 is
  ProxyType=OnPremise). Env pins `MODEL_NAME=gpt-5.2`,
  `LLM_DEPLOYMENT_ID=dcc9a836b894dc1d`, `S4HANA_DESTINATION_NAME=S43`,
  `S4HANA_ODATA_BASE_PATH=/sap/opu/odata/sap`, `KB_BACKEND=local`,
  `KB_REBUILD_ON_START=true`.
- `requirements.txt` first line is a bare `.`; regen with
  `uv export --no-hashes --no-dev -o requirements.txt`. `runtime.txt` pins
  `python-3.13.x`. `PORT` is injected by CF — do not set it.
- Joule webclient the coworker deployed to:
  `https://gen-ai.us10.sapdas.cloud.sap/webclient/standalone/finance_ai_chatbot`.
- To check what a deployed instance actually runs: `GET /.well-known/agent.json`,
  `/diag/domains`, `/diag/s4/catalog`, or ask it "what can you help me with?".

## Regression question bank (ask the live/deployed bot)

Acceptance for this session's fix:
- "Has payment been sent for invoice 5100000016?" → real cleared/not-cleared, or
  a clean "clearing lookup isn't available on this system, contact AP Payments"
  — never a raw 400.
- "Has accounting document 1900000001 in company code 1710, FY 2017 been
  cleared?" → same.

Broader coverage (swap in real IDs from `/diag/s4/samples`):
- `get_invoice_payment_status`: "Has invoice 5100000017 been paid?", "What
  accounting document was created for invoice 5100000017?", "What payment run
  was invoice 5100000016 in?"
- `get_invoice_items`: "What is invoice 5100000017 made up of?" (must not ask for
  the fiscal year).
- `check_three_way_match`: "Did invoice 5100000017 pass the 3-way match?", "Why
  is invoice X blocked for payment?"
- `get_purchase_order_items` / `_delivery_schedule` / `_approval_status`:
  "line items on PO X", "when is PO X due", "has PO X been approved?" ("who
  approves next" → release status + point to workflow log, never a name).
- `get_goods_receipts_for_po`: "Has PO X been received?"
- `get_budget_status`: "Is there budget for PO X?" → `budgetAvailable=false` +
  report pointer, never a number.
- Behaviour: bare number "status of 4500001234?" → ask PO vs invoice vs
  accounting doc; unknown ID → plain "no such record"; "release PO X for me" →
  refuse (read-only); "3-way match tolerance?" → `search_policy_docs`, not the
  match tool.
- **PII exceptions (2026-09, explicit product sign-off — not the default):**
  `get_vendor_email_addresses` and `get_vendor_bank_accounts` are the only two
  tools allowed to return email / IBAN-bank-account data; every other lookup
  still strips both (`guardrails.SENSITIVE_KEYS`). "Bank account for vendor
  1000000" now answers (IBAN/bank key/SWIFT/account holder) instead of
  refusing — call ONLY when explicitly asked, and only for that vendor's own
  payment-routing data (never tax id / phone / home address, even from these
  two tools). "Bank statement" (TR-CM transactions) is a different, still
  unconnected thing — no live source exists; stays a handoff.
