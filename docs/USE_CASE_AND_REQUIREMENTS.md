# AHF Finance ChatBot — Use Case & Requirements

Companion to `AI_Chatbot_Finance_FAQ_Design_Approach.docx` (Anish Kothuri /
Pradeep Murugesan, for Karthik Byju). That doc is the architecture decision;
this one is the working scope + readiness checklist the build tracks against.

---

## 1. Use case

Finance teams field a high volume of repetitive questions — invoice status, T&E
policy, PO approval thresholds, vendor onboarding, "which tax code do I use",
"how do I request a cost center". The chatbot answers these **instantly from
grounded knowledge**, gives live read-only status from S/4HANA where it is
connected, points people at the right document or team, and **escalates to a
human when it is not confident**.

- **Users:** AHF accounts-payable, procurement, and finance staff.
- **Channel:** SAP Joule, reached from Teams / S/4HANA. This repo is an A2A
  server; Joule is the A2A client (capability in `joule-capability/`).
- **Model:** GPT 5.2 via SAP Generative AI Hub (AI Core). Embeddings:
  `text-embedding-3-small` (optional; local fallback otherwise).
- **Grounding:** RAG over approved finance SOPs + read-only S/4HANA OData.

### Non-goals (unchanged from the design approach)

- **No write-back to S/4HANA** — no bot-initiated approvals, postings,
  releases, payments, blocks/unblocks. Ever.
- No PII or vendor/customer banking data in responses.
- No multi-turn financial analysis, forecasting, or advice.
- Nothing outside AP / procurement / finance.

---

## 2. What "works like a chatbot" means (conversation requirements)

| # | Requirement | Status |
|---|---|---|
| C1 | **Multi-turn context** — a follow-up ("is it blocked?", "what about FY2025?") is answered against the prior turns of the same conversation. | ✅ `agent_executor._history_for_generate` replays `task.history`; `answering.sanitize_history` trims it (`CHAT_HISTORY_MAX_MESSAGES`, default 10). |
| C2 | **Grounded answers only** — every policy answer comes from a retrieved passage; every record status comes from a live lookup. No answering finance questions from model general knowledge. | ✅ system prompt + `search_policy_docs` `grounded` flag + S/4HANA tools. |
| C3 | **Citations** — policy answers cite `(<title> — <section>)`. | ✅ prompt contract + tool message. |
| C4 | **Escalate on low confidence / no source** — hand off to the AHF finance support team rather than guess. | ✅ `grounded=false` path, `escalation.py`; confidence bar is step 6. |
| C5 | **Honest about scope** — for a finance area with no live lookup yet, answer the policy part and say the live figure isn't connected. | ✅ prompt "Scope of live status lookups" block. |
| C6 | **Read-only + no sensitive data** — structurally GET-only; outbound scrub. | ✅ `s4hana.py` (GET only), `guardrails.py`. |
| C7 | **Never terminal** — the A2A task stays `input_required` so Joule follow-ups keep working. | ✅ `agent_executor`. |
| C8 | **Degrade, don't crash** — LLM down → honest fallback; tool error → handed back to the model. | ✅ `answering.generate`. |
| C9 | **60s A2A ceiling** — slow lookups use the async webhook path. | ⚠️ push path wired (step 5); per-call timeouts keep sync calls safe. |
| C10 | **Observability from day one** — one interaction record per turn (grounded / escalated / tools / kb_hits / latency). | ✅ `observability.py`; AI Core inference observability is an AI Core setting. |

---

## 3. Finance domain coverage

The agent is scoped to the finance areas below (the design image list, plus the
AP/procurement/vendor areas already built). Source of truth in code:
`src/ahf_finance_agent/domains.py` — served live at `GET /diag/domains`.

**Status key:** `live` = read-only S/4HANA lookup wired · `kb_only` = policy /
process answered from the knowledge base, no live status lookup yet.

| Domain | SAP area | Status | S/4HANA OData service(s) | KB doc | Example question |
|---|---|---|---|---|---|
| Accounts Payable | FI-AP | **live** | `API_SUPPLIERINVOICE_PROCESS_SRV` (header + `A_SuplrInvcItemPurOrdRef` items) | `invoice_payment_terms.md` | "Status of invoice 5105601234?" / "What are its line items?" |
| Procurement (PO / PR) | MM-PUR | **live** | `API_PURCHASEORDER_PROCESS_SRV` (status + release/approval), `API_PURCHASE_REQUISITION_SRV` | `po_approval_thresholds.md` | "Has PO 4500001234 been approved / released?" |
| Goods Receipt / Material Docs | MM-IM | **live** | `API_MATERIAL_DOCUMENT_SRV` (`A_MaterialDocumentItem` by PO) | `po_approval_thresholds.md` | "Has PO 4500001234 been received?" |
| Invoice Verification / 3-way match | MM-IV | **live** (computed) | `API_SUPPLIERINVOICE_PROCESS_SRV` + `API_PURCHASEORDER_PROCESS_SRV` + `API_MATERIAL_DOCUMENT_SRV` (no API for SAP's own stored match result) | `invoice_payment_terms.md` | "Did invoice 5105601234 pass the three-way match?" |
| Vendor / Business Partner | MDG-BP | **live** | `API_BUSINESS_PARTNER` (incl. `A_BusinessPartnerAddress` / `A_AddressEmailAddress` and `A_BusinessPartnerBank` — two deliberate PII exceptions, see `get_vendor_email_addresses` / `get_vendor_bank_accounts`) | `vendor_onboarding.md` | "Is vendor 100000 set up?" / "What's the contact email for vendor 100000?" / "What's the IBAN on file for vendor 100000?" |
| Payments & clearing (cross AP/AR/TR) | FI / TR-CM | **live** | `API_OPLACCTGDOCITEMCUBE_SRV` (falls back to `API_JOURNALENTRYITEMBASIC_SRV`) | `invoice_payment_terms.md` | "Has acct doc 1400000123 (FY2026) cleared?" |
| Journal Entry / G/L postings | FI-GL | kb_only | `API_JOURNALENTRYITEMBASIC_SRV`, `API_GLACCOUNTLINEITEM` | `general_ledger_and_journal_entries.md` | "How do I request a new G/L account?" |
| G/L Accounts & balances | FI-GL | kb_only | `API_GLACCOUNTLINEITEM`, `API_GLACCTBALANCE_SRV` | `general_ledger_and_journal_entries.md` | "Balance on 400000 for CC 1710?" *(policy only)* |
| Accounts Receivable | FI-AR | kb_only | `API_CUSTOMER_INVOICE_SRV`, `API_OPLACCTGDOCITEMCUBE_SRV` | `accounts_receivable.md` | "What is our dunning process?" |
| Cost Centers (CO-CCA) | CO-CCA | kb_only | `API_COSTCENTER_SRV` | `cost_and_profit_centers.md` | "How do I request a new cost center?" |
| Profit Centers (CO-PCA) | CO-PCA | kb_only | `API_PROFITCENTER_SRV` | `cost_and_profit_centers.md` | "Cost center vs profit center here?" |
| Fixed Assets (FI-AA) | FI-AA | kb_only | `API_FIXEDASSET_SRV` | `fixed_assets.md` | "What's the capitalization threshold?" |
| Bank & Cash (TR-CM) | TR-CM | kb_only | `API_BANKSTATEMENT_SRV`, `API_HOUSEBANK_SRV` | `bank_and_cash.md` | "When are bank statements reconciled?" |
| Tax | FI tax | kb_only | `API_TAXCODE_SRV` | `tax.md` | "Which tax code for a domestic services PO?" |

> OData service names for `kb_only` domains are **best-guess targets** — confirm
> the exact service alias against the S/4HANA system's `/IWFND/MAINT_SERVICE`
> and the API Business Hub before wiring.

---

## 4. What we need to connect a domain (per `kb_only` row)

Each domain flips to `live` by adding **one tool** — no redesign. Prerequisites:

1. **OData service activated + authorised** on the S/4HANA system
   (`/IWFND/MAINT_SERVICE`), and the agent's service user granted read
   authorisation for it. Several already-built tools (`get_payment_clearing_status`,
   PO, PR) sit behind this same gate.
2. **`$select` allow-list** for that entity set — header / status / business
   fields only, **no bank / tax-id / personal fields** (mirror the lists in
   `s4hana.py`). This is where PII exclusion lives.
3. **MCP Gateway registration** (design-approach target): register the service
   in SAP Integration Suite's MCP Gateway as a business-level tool, secured by
   OAuth2 client credentials, reached via a `MCP_GATEWAY` BTP destination.
   Until then, direct OData through the `S43` destination is the stop-gap.
4. **Code:** add the lookup method to `s4hana.py`, the handler + schema to
   `tools.py` (`_HANDLERS` / `TOOL_SPECS`), a line to the system prompt's
   S/4HANA tool list, and flip the domain's `status` to `"live"` in
   `domains.py`. Add a `field_maps`-style code→label table for any raw status
   codes the service returns.
5. **Tests:** a `dispatch_tool` test + an answering-loop test (see
   `tests/test_tools.py`, `tests/test_answering.py`).

### Knowledge base (all domains, now)

- `knowledge_base/docs/` holds **SAMPLE / PLACEHOLDER** policy content for every
  domain so retrieval works end to end today. **Finance stakeholders must
  replace each file with the approved document** before pilot — the bot quotes
  and cites them verbatim. Document curation is the real critical path.
- One topic per file, real `#`/`##` headings (they become citation sections),
  no PII / banking data.
- Rebuild the index after any change: `python -m ahf_finance_agent.kb_ingest`
  (also runs on server start when the index is missing / `KB_REBUILD_ON_START`).
- Production embeddings: set `EMBEDDING_DEPLOYMENT_ID` to a
  `text-embedding-3-small` deployment, rebuild the index, and raise
  `KB_MIN_SCORE` (try `0.75`).

### Still SAP-side (from the design approach, not in this repo)

- IAS App2App trust between Joule and this agent (replaces the dev
  `NoAuthentication` hop).
- Joule Scenario + Dialog Function pointed at the deployed A2A endpoint.
- HANA Cloud vector store, if the KB is to move off the local index
  (`KB_BACKEND=hana` — `_HanaCloudBackend` is a wired stub).

---

## 5. Readiness checklist

**Set up for success now (done):**

- [x] Multi-turn conversation wired (C1) and all C-row requirements above.
- [x] All 14 finance domains enumerated in `domains.py`, surfaced at `/diag/domains`.
- [x] SAMPLE knowledge-base doc for every domain; index builds on deploy.
- [x] System prompt covers the full scope and the live-vs-KB-only distinction.
- [x] Adding a domain's live lookup is an additive change (tool + one prompt line + status flip).
- [x] `/ready` reports KB chunk count, embedding backend, domain coverage.

**Per domain, before it counts as `live`:**

- [ ] OData service confirmed active + authorised on S/4HANA.
- [ ] `$select` allow-list reviewed for PII (finance + security sign-off).
- [ ] Tool + schema + prompt line + `domains.py` status + `field_maps` added.
- [ ] Tests added; `/diag/s4`-style smoke passes against the live service.
- [ ] (Target) service registered in MCP Gateway; `USE_MCP_TOOLS` path exercised.

**Before pilot:**

- [ ] Every `knowledge_base/docs/*.md` replaced with an approved document.
- [ ] `EMBEDDING_DEPLOYMENT_ID` set; index rebuilt; `KB_MIN_SCORE` retuned.
- [ ] IAS App2App trust in place; `APP_ENV=prod`.
- [ ] AI Core inference observability enabled; `INTERACTION_LOG_PATH` set.
- [ ] Small-group pilot: measure answer accuracy and escalation rate.

---

## 6. Current state (this branch)

Live read-only lookups: supplier invoice (header + line items), payment/clearing
(by FI document, and by invoice number via `get_invoice_payment_status`, which
resolves the FI document itself), PO (status + release/approval + line items + delivery schedule),
goods receipts against a PO, a computed PO-GR-invoice 3-way match
(`check_three_way_match`), PR, vendor.
Still knowledge-base-only within procure-to-pay: the dedicated PO
approval-workflow service (`API_PURCHASE_ORDER_APPROVAL_SRV` — release state is
read off the PO API for now), the formal match / block-release workflow history
(`API_PO_INVOICE_MATCH_SRV` — the comparison is computed instead), payment RUN /
F110 proposal IDs, payment documents (`API_PAYMENT_DOCUMENT_SRV`) and bank
statements (`API_BANK_STATEMENT_SRV`).
Policy Q&A: all 14 domains, from SAMPLE docs, via `search_policy_docs` with
citations and human handoff when nothing relevant is retrieved. Multi-turn
context: on. Everything else above is a tracked prerequisite, not a code gap.
