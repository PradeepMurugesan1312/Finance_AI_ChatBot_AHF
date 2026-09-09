"""Agent Card — the identity and capability list an A2A client (Joule) reads
from ``/.well-known/agent.json`` to decide when to route a user request here.

Every skill below is a *read*; there is deliberately no skill that approves,
posts, releases, or pays anything.

IMPORTANT — keep this in lock-step with :mod:`ahf_finance_agent.tools`. This
list drifted badly once already: it was written for the original 6-tool
Step-3 build and never updated across Step 4 and the later procure-to-pay /
master-data widening, so by the time 20 S/4HANA tools existed, Joule's
capability discovery still only advertised 6 of them (plus the policy tool) —
a silent gap that looked like "the new tools don't work" when the backend was
actually fine. The assertion at the bottom of this module makes that class of
drift fail fast (``pytest`` / import time) instead of silently shipping stale.
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from ahf_finance_agent.tools import POLICY_TOOL_NAME, TOOL_NAMES

AGENT_NAME = "AHF Finance AI ChatBot"
AGENT_VERSION = "0.1.0"

_DESCRIPTION = (
    "Answers routine finance questions for AHF accounts-payable, procurement, "
    "and finance staff. Provides live read-only status of supplier invoices "
    "(incl. line items and payment/clearing), purchase orders and "
    "requisitions (incl. items, delivery schedule, approval, three-way "
    "match), goods receipts, vendor / business partner master data (incl. "
    "email and bank details, on request only), budget/availability-control, "
    "and company code / cost centre / profit centre / G/L account master "
    "data and activity — all from SAP S/4HANA — and answers AP / procurement "
    "/ finance policy and process questions grounded in approved company "
    "documents, with citations. "
    "Strictly read-only: it never approves, posts, releases, or pays anything, "
    "and never returns personal data or vendor banking details. Escalates to a "
    "human finance queue when it is not confident or the answer is not in the "
    "approved documents."
)


def _skills() -> list[AgentSkill]:
    return [
        AgentSkill(
            id="get_invoice_status",
            name="Get Invoice Status",
            description=(
                "Look up the status and header details of a specific supplier "
                "invoice in SAP S/4HANA by invoice number and fiscal year "
                "(payment terms, blocking reason, PO match)."
            ),
            tags=["accounts payable", "invoice", "status", "read-only"],
            examples=[
                "What's the status of invoice 5105601234 for fiscal year 2026?",
                "Has invoice 5105601234 been paid?",
            ],
        ),
        AgentSkill(
            id="get_invoice_items",
            name="Get Invoice Line Items",
            description=(
                "List the purchase-order-referenced line items on a supplier "
                "invoice in SAP S/4HANA (PO + item, quantity, amount, tax code)."
            ),
            tags=["accounts payable", "invoice", "line items", "read-only"],
            examples=["What is invoice 5105601234 made up of?", "Which PO does invoice 5105601234 bill against?"],
        ),
        AgentSkill(
            id="search_invoices_by_vendor",
            name="Search Invoices by Vendor",
            description=(
                "List recent supplier invoices and their status for a given "
                "vendor in SAP S/4HANA."
            ),
            tags=["accounts payable", "invoice", "vendor", "read-only"],
            examples=[
                "Show me recent invoices from vendor 100000",
                "What invoices are open for supplier 100000 in company code 1000?",
            ],
        ),
        AgentSkill(
            id="get_payment_clearing_status",
            name="Get Payment Clearing Status",
            description=(
                "Confirm whether a payment has actually posted (cleared) "
                "against an accounting document in SAP S/4HANA, as opposed to "
                "the invoice merely being parked / posted / blocked."
            ),
            tags=["accounts payable", "payment", "clearing", "read-only"],
            examples=[
                "Has payment cleared for accounting document 1400000123, fiscal year 2026?",
                "Did we actually pay out accounting document 1400000123 yet?",
            ],
        ),
        AgentSkill(
            id="get_invoice_payment_status",
            name="Get Invoice Payment Status",
            description=(
                "Determine whether payment has been sent for a supplier invoice "
                "in SAP S/4HANA, by invoice number alone — resolves the FI "
                "accounting document and checks clearing for you."
            ),
            tags=["accounts payable", "payment", "invoice", "read-only"],
            examples=[
                "Has invoice 5100000016 been paid?",
                "What accounting document was created for invoice 5100000017?",
            ],
        ),
        AgentSkill(
            id="get_purchase_order_status",
            name="Get Purchase Order Status",
            description=(
                "Look up a purchase order's status, approval state, and header "
                "details in SAP S/4HANA."
            ),
            tags=["procurement", "purchase order", "status", "read-only"],
            examples=[
                "What's the status of PO 4500001234?",
                "Has purchase order 4500001234 been approved?",
            ],
        ),
        AgentSkill(
            id="get_purchase_order_items",
            name="Get Purchase Order Line Items",
            description=(
                "List a purchase order's line items in SAP S/4HANA: ordered "
                "quantity, unit, net price and price unit per item."
            ),
            tags=["procurement", "purchase order", "line items", "read-only"],
            examples=["What are the line items on PO 4500001234?"],
        ),
        AgentSkill(
            id="get_purchase_order_delivery_schedule",
            name="Get Purchase Order Delivery Schedule",
            description=(
                "Get the delivery date(s) for a purchase order in SAP S/4HANA "
                "from its schedule lines (the header and items carry none)."
            ),
            tags=["procurement", "purchase order", "delivery", "read-only"],
            examples=["When is PO 4500001234 due?", "What's the delivery date on PO 4500001234?"],
        ),
        AgentSkill(
            id="get_purchase_order_approval_status",
            name="Get Purchase Order Approval Status",
            description=(
                "Check specifically whether a purchase order in SAP S/4HANA is "
                "fully released / approved, or is still awaiting further sign-off."
            ),
            tags=["procurement", "purchase order", "approval", "read-only"],
            examples=["Has PO 4500001234 been released / approved?", "Is PO 4500001234 still waiting for sign-off?"],
        ),
        AgentSkill(
            id="check_three_way_match",
            name="Check Three-Way Match",
            description=(
                "Assess whether a supplier invoice matches its purchase order "
                "and goods receipt in SAP S/4HANA (three-way match), including "
                "any quantity/price variance and the invoice's payment block."
            ),
            tags=["invoice verification", "three-way match", "read-only"],
            examples=["Did invoice 5105601234 pass the three-way match?", "Why is invoice 5105601234 blocked for payment?"],
        ),
        AgentSkill(
            id="get_goods_receipts_for_po",
            name="Get Goods Receipts for PO",
            description=(
                "List the goods receipts (inbound material documents) posted "
                "against a purchase order in SAP S/4HANA."
            ),
            tags=["goods receipt", "procurement", "read-only"],
            examples=["Has PO 4500001234 been received?", "What goods receipts were posted against PO 4500001234?"],
        ),
        AgentSkill(
            id="get_purchase_requisition_status",
            name="Get Purchase Requisition Status",
            description=(
                "Look up a purchase requisition's status and header details in "
                "SAP S/4HANA."
            ),
            tags=["procurement", "purchase requisition", "status", "read-only"],
            examples=["What's the status of purchase requisition 1000005678?"],
        ),
        AgentSkill(
            id="get_vendor_details",
            name="Get Vendor Details",
            description=(
                "Look up a vendor's (business partner's) master data in SAP "
                "S/4HANA to confirm onboarding / setup status. Never returns "
                "bank account, IBAN, tax id, or personal contact details."
            ),
            tags=["vendor", "business partner", "onboarding", "read-only"],
            examples=[
                "Is vendor 100000 set up in the system?",
                "What's the onboarding status for business partner 100000?",
            ],
        ),
        AgentSkill(
            id="get_vendor_email_addresses",
            name="Get Vendor Email Addresses",
            description=(
                "Look up the email address(es) on file for a vendor / business "
                "partner in SAP S/4HANA. The only skill allowed to return an "
                "email address; used only when explicitly asked."
            ),
            tags=["vendor", "contact", "read-only"],
            examples=["What's the contact email for vendor 100000?"],
        ),
        AgentSkill(
            id="get_vendor_bank_accounts",
            name="Get Vendor Bank Accounts",
            description=(
                "Look up the bank account / payment-routing details on file for "
                "a vendor in SAP S/4HANA (IBAN, bank key, SWIFT, account "
                "holder). The only skill allowed to return this data; used "
                "only when explicitly asked. Not a bank statement."
            ),
            tags=["vendor", "bank", "read-only"],
            examples=["What's the IBAN / bank account on file for vendor 100000?"],
        ),
        AgentSkill(
            id="get_budget_status",
            name="Get Budget Status",
            description=(
                "Check budget / availability-control status for a cost centre, "
                "internal order, or PO account assignment. The budget/plan "
                "figure itself is never available on this tenant; returns "
                "computed actual spend and, for a PO, its committed value."
            ),
            tags=["budgeting", "cost center", "read-only"],
            examples=["Is there budget left for purchase order 4500001234?", "Is internal order 700123 over budget?"],
        ),
        AgentSkill(
            id="get_company_code_details",
            name="Get Company Code Details",
            description=(
                "Look up a company code's master data in SAP S/4HANA: name, "
                "country, currency, chart of accounts, fiscal year variant."
            ),
            tags=["general ledger", "company code", "read-only"],
            examples=["What currency and chart of accounts does company code 1710 use?"],
        ),
        AgentSkill(
            id="get_cost_center_details",
            name="Get Cost Center Details",
            description=(
                "Look up a cost centre's master data in SAP S/4HANA: validity, "
                "responsible person, category, and assigned profit centre."
            ),
            tags=["cost center", "CO-CCA", "read-only"],
            examples=[
                "Who is responsible for cost centre 1000?",
                "Is cost centre 1000 still valid, and what profit centre is it assigned to?",
            ],
        ),
        AgentSkill(
            id="get_profit_center_details",
            name="Get Profit Center Details",
            description=(
                "Look up a profit centre's master data in SAP S/4HANA: "
                "responsible person, segment, block status, validity."
            ),
            tags=["profit center", "CO-PCA", "read-only"],
            examples=["Is profit centre 1000 still valid, and what's its description?"],
        ),
        AgentSkill(
            id="get_gl_account_master",
            name="Get G/L Account Master Data",
            description=(
                "Look up a G/L account's master data in SAP S/4HANA: account "
                "group, balance-sheet vs P&L, block status. Not its balance — "
                "use Get G/L Account Activity for that."
            ),
            tags=["general ledger", "gl account", "read-only"],
            examples=["Is G/L account 400000 a balance sheet or P&L account?"],
        ),
        AgentSkill(
            id="get_gl_account_activity",
            name="Get G/L Account Activity",
            description=(
                "Compute the net posted amount on a G/L account in a company "
                "code from live journal-entry postings in SAP S/4HANA. Not an "
                "official trial-balance / period-end figure."
            ),
            tags=["general ledger", "gl account", "balance", "read-only"],
            examples=["What is the balance on G/L account 400000 for company code 1710?"],
        ),
        AgentSkill(
            id="get_accounts_payable_summary",
            name="Get Accounts Payable Summary",
            description=(
                "Portfolio-level AP: computed count and net amount of open "
                "(not yet cleared) vendor postings for a company code — 'how "
                "much do we owe' — as opposed to a single invoice lookup. "
                "Directional, not an official aging report."
            ),
            tags=["accounts payable", "summary", "read-only"],
            examples=[
                "What's our total accounts payable for company code 1710?",
                "How much do we currently owe vendor 100000?",
            ],
        ),
        AgentSkill(
            id="get_accounts_receivable_summary",
            name="Get Accounts Receivable Summary",
            description=(
                "Portfolio-level AR: computed count and net amount of open "
                "customer postings for a company code — 'how much are we "
                "owed'. May report not-connected on this tenant (unconfirmed "
                "field support); directional, not an official aging report."
            ),
            tags=["accounts receivable", "summary", "read-only"],
            examples=["What's our total accounts receivable for company code 1710?"],
        ),
        AgentSkill(
            id="count_purchase_orders",
            name="Count Purchase Orders",
            description=(
                "Count of purchase orders matching a scope: a date range "
                "(created), pending approval, or one vendor — a computed, "
                "possibly-capped count, not a list of the POs themselves."
            ),
            tags=["purchase order", "count", "volume", "read-only"],
            examples=[
                "How many purchase orders were created last week?",
                "How many POs are still pending approval right now?",
            ],
        ),
        AgentSkill(
            id="count_purchase_requisitions",
            name="Count Purchase Requisitions",
            description=(
                "Count of purchase requisitions created in a date range. May "
                "report not-connected on this tenant (a known PR "
                "authorisation gap), independent of this specific count."
            ),
            tags=["purchase requisition", "count", "volume", "read-only"],
            examples=["How many purchase requisitions were raised this month?"],
        ),
        AgentSkill(
            id="count_supplier_invoices",
            name="Count Supplier Invoices",
            description=(
                "Count of supplier invoices matching a scope: a date range "
                "(posted), payment-blocked status, one vendor, or one "
                "company code."
            ),
            tags=["invoice", "count", "volume", "read-only"],
            examples=[
                "Count of supplier invoices received last week.",
                "How many invoices are currently blocked for payment?",
            ],
        ),
        AgentSkill(
            id="count_invoices_by_fiscal_period",
            name="Count Invoices By Fiscal Period",
            description=(
                "Count of vendor-invoice accounting documents posted in one "
                "company code / fiscal year / fiscal period."
            ),
            tags=["invoice", "fiscal period", "count", "read-only"],
            examples=["How many invoices were posted in fiscal period 5?"],
        ),
        AgentSkill(
            id="count_goods_receipts",
            name="Count Goods Receipts",
            description=(
                "Count of distinct goods-receipt documents posted in a date "
                "range, optionally for one PO. Does not filter to receipts "
                "against still-open POs."
            ),
            tags=["goods receipt", "count", "volume", "read-only"],
            examples=["How many goods receipts were posted against open POs this week?"],
        ),
        AgentSkill(
            id="count_pos_overdue_without_goods_receipt",
            name="Count Overdue POs Without Goods Receipt",
            description=(
                "Best-effort, explicitly sampled (not exhaustive) count of "
                "purchase orders with an overdue delivery date and no goods "
                "receipt posted yet — slower than the other count tools."
            ),
            tags=["purchase order", "goods receipt", "overdue", "count", "read-only"],
            examples=["How many POs have no goods receipt yet, even though they're past their delivery date?"],
        ),
        AgentSkill(
            id="count_cleared_documents",
            name="Count Cleared Accounting Documents",
            description=(
                "Count of distinct accounting documents cleared in a date "
                "range, optionally for one vendor (e.g. 'invoices paid to "
                "vendor X this month')."
            ),
            tags=["clearing", "payments", "count", "volume", "read-only"],
            examples=[
                "Count of accounting documents cleared last week.",
                "How many invoices were paid out to vendor 1000502 this month?",
            ],
        ),
        AgentSkill(
            id="count_new_vendors",
            name="Count New Vendors",
            description="Count of vendors (suppliers) created in a date range.",
            tags=["vendor", "onboarding", "count", "read-only"],
            examples=["How many new vendors were onboarded this quarter?"],
        ),
        AgentSkill(
            id="count_blocked_vendors",
            name="Count Blocked Vendors",
            description="Count of vendors currently blocked for posting or purchasing.",
            tags=["vendor", "blocked", "count", "read-only"],
            examples=["How many vendors are currently blocked for posting or purchasing?"],
        ),
        AgentSkill(
            id="search_vendors_by_name",
            name="Search Vendors By Name",
            description=(
                "Resolve a vendor/supplier NAME to its supplier number(s) — "
                "every other vendor-related tool here needs a number, not a "
                "name."
            ),
            tags=["vendor", "search", "read-only"],
            examples=["How many purchase orders were created for vendor Cosmo Energy Holdings this year?"],
        ),
        AgentSkill(
            id="search_policy_docs",
            name="Answer Finance Policy Questions",
            description=(
                "Answer accounts-payable, procurement, and general finance "
                "policy / process questions (T&E rules, PO and PR approval "
                "thresholds, vendor onboarding steps, invoice payment terms "
                "and match tolerances) from approved company documents, with "
                "citations and a link to the source document. Says so and "
                "points to a human when the policy is not in the knowledge base."
            ),
            tags=["policy", "faq", "procedure", "accounts payable", "procurement"],
            examples=[
                "What's the approval threshold for a purchase order?",
                "How many days do I have to submit an expense report?",
                "What are the steps to onboard a new vendor?",
                "What are our standard invoice payment terms?",
            ],
        ),
    ]


# Fail fast (import time) if a tool is ever added/renamed in tools.py without a
# matching AgentSkill here, or vice versa — this exact drift (agent.json stuck
# advertising 6 tools while tools.py grew to 20+) is what made Joule unable to
# route several already-working S/4HANA lookups.
_SKILL_IDS = frozenset(s.id for s in _skills())
assert _SKILL_IDS == TOOL_NAMES | {POLICY_TOOL_NAME}, (
    f"agent_card._skills() is out of sync with tools.TOOL_NAMES — "
    f"missing from agent card: {TOOL_NAMES | {POLICY_TOOL_NAME} - _SKILL_IDS}, "
    f"stale entries in agent card: {_SKILL_IDS - (TOOL_NAMES | {POLICY_TOOL_NAME})}"
)


def get_agent_card(base_url: str) -> AgentCard:
    """Build the Agent Card served at ``/.well-known/agent.json``.

    ``base_url`` is the agent's public origin (no trailing slash), e.g.
    ``https://finance-ai-chatbot.cfapps.us10-001.hana.ondemand.com``.
    """
    return AgentCard(
        name=AGENT_NAME,
        description=_DESCRIPTION,
        url=f"{base_url.rstrip('/')}/",
        version=AGENT_VERSION,
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=True),
        skills=_skills(),
    )
