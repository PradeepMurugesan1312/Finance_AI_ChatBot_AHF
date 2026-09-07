"""Finance domain registry — the full scope the chatbot is meant to cover.

The design approach (``AI_Chatbot_Finance_FAQ_Design_Approach.docx``) scopes the
assistant to "accounts payable, procurement, and general finance FAQs". In
practice that spans the finance areas below. Only a subset has **live**
read-only S/4HANA lookups wired today (build step 3); the rest are answerable
from the **policy knowledge base** now and gain live status lookups as their
OData services are connected — with no change to the agent's shape, only new
entries in :mod:`ahf_finance_agent.tools`.

``status`` values:
* ``live``          — a read-only S/4HANA lookup tool exists and is wired.
* ``kb_only``       — policy / process questions answered from the KB; no live
                      status lookup yet (OData service not connected).
* ``planned``       — on the roadmap; KB coverage may be thin.

Keeping this here (rather than only in prose) lets ``/ready`` report coverage
and gives the next developer one obvious place to flip a domain to ``live``
when its tool lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FinanceDomain:
    key: str
    name: str
    sap_area: str
    status: str  # "live" | "kb_only" | "planned"
    odata_services: tuple[str, ...]
    example_questions: tuple[str, ...]
    kb_docs: tuple[str, ...] = field(default_factory=tuple)


FINANCE_DOMAINS: tuple[FinanceDomain, ...] = (
    FinanceDomain(
        key="accounts_payable",
        name="Accounts Payable",
        sap_area="FI-AP",
        status="live",
        odata_services=("API_SUPPLIERINVOICE_PROCESS_SRV", "API_SUPPLIER_INVOICE_ITEM_SRV", "API_OPLACCTGDOCITEMCUBE_SRV"),
        example_questions=(
            "What's the status of invoice 5105601234?",
            "Is invoice 5105601234 blocked for payment, and why?",
            "What are the line items on invoice 5105601234?",
            "Show me recent invoices from vendor 100000.",
            "What's our total accounts payable for company code 1710?",
            "How much do we currently owe vendor 100000?",
            "What are our standard supplier payment terms?",
        ),
        kb_docs=("invoice_payment_terms.md",),
    ),
    FinanceDomain(
        key="procurement",
        name="Procurement (PO / PR)",
        sap_area="MM-PUR",
        status="live",
        odata_services=(
            "API_PURCHASEORDER_PROCESS_SRV",
            "API_PURCHASE_ORDER_APPROVAL_SRV",
            "API_PURCHASE_REQUISITION_SRV",
        ),
        example_questions=(
            "What's the status of PO 4500001234?",
            "Has PO 4500001234 been released / approved?",
            "Has purchase requisition 1000005678 been approved?",
            "What's the approval threshold for a purchase order?",
        ),
        kb_docs=("po_approval_thresholds.md",),
    ),
    FinanceDomain(
        key="goods_receipt",
        name="Goods Receipt / Material Documents",
        sap_area="MM-IM",
        status="live",
        odata_services=("API_MATERIAL_DOCUMENT_SRV", "API_GOODS_RECEIPT_SRV"),
        example_questions=(
            "Has PO 4500001234 been received?",
            "What goods receipts were posted against PO 4500001234?",
            "Was the delivery for PO 4500001234 booked or later cancelled?",
        ),
        kb_docs=("po_approval_thresholds.md",),
    ),
    FinanceDomain(
        key="invoice_verification",
        name="Invoice Verification / 3-Way Match",
        sap_area="MM-IV",
        status="live",
        # No live API for SAP's own stored match result / block-release history
        # (API_PO_INVOICE_MATCH_SRV). check_three_way_match computes the
        # comparison from the invoice, PO and goods-receipt APIs and reads the
        # resulting payment block off the invoice header.
        odata_services=(
            "API_SUPPLIERINVOICE_PROCESS_SRV",
            "API_PURCHASEORDER_PROCESS_SRV",
            "API_MATERIAL_DOCUMENT_SRV",
        ),
        example_questions=(
            "Did invoice 5105601234 pass the three-way match?",
            "Why is invoice 5105601234 showing a quantity variance?",
            "What tolerance applies to a price difference on an invoice?",
        ),
        kb_docs=("invoice_payment_terms.md",),
    ),
    FinanceDomain(
        key="vendor_master",
        name="Vendor / Business Partner",
        sap_area="MDG-BP",
        status="live",
        odata_services=("API_BUSINESS_PARTNER",),
        example_questions=(
            "Is vendor 100000 set up in the system?",
            "What are the steps to onboard a new vendor?",
            "What's the contact email for vendor 100000?",
            "What's the IBAN / bank account on file for vendor 100000?",
        ),
        kb_docs=("vendor_onboarding.md",),
    ),
    FinanceDomain(
        key="payments",
        name="Payments & Clearing (cross AP/AR/TR)",
        sap_area="FI / TR-CM",
        status="live",
        odata_services=(
            "API_JOURNALENTRYITEMBASIC_SRV",
            "API_OPLACCTGDOCITEMCUBE_SRV",
        ),
        example_questions=(
            "Has payment cleared for accounting document 1400000123, fiscal year 2026?",
            "Did we actually pay out accounting document 1400000123 yet?",
            "How and when was invoice 5100000016 paid (payment document, method, bank)?",
        ),
        kb_docs=("invoice_payment_terms.md",),
    ),
    FinanceDomain(
        key="general_ledger",
        name="General Ledger / Journal Entries & G/L postings",
        sap_area="FI-GL",
        status="live",
        odata_services=("API_OPLACCTGDOCITEMCUBE_SRV", "API_JOURNALENTRYITEMBASIC_SRV", "API_COMPANYCODE_SRV"),
        example_questions=(
            "How do I request a new G/L account?",
            "When does the accounting period close each month?",
            "What's the policy for parking vs posting a journal entry?",
            "What currency and chart of accounts does company code 1710 use?",
        ),
        kb_docs=("general_ledger_and_journal_entries.md",),
    ),
    FinanceDomain(
        key="gl_accounts_balances",
        name="G/L Accounts & Balances",
        sap_area="FI-GL",
        status="live",
        # get_gl_account_activity computes a posting total from journal_entry_item
        # (NOT an official trial-balance figure); get_gl_account_master gives the
        # account's classification (balance-sheet vs P&L, account group).
        odata_services=("API_OPLACCTGDOCITEMCUBE_SRV", "API_GLACCOUNTINCHARTOFACCOUNTS_SRV"),
        example_questions=(
            "What is the balance on G/L account 400000 for company code 1710?",
            "Is G/L account 400000 a balance sheet or P&L account?",
            "How do I read the trial balance report?",
        ),
        kb_docs=("general_ledger_and_journal_entries.md",),
    ),
    FinanceDomain(
        key="accounts_receivable",
        name="Accounts Receivable",
        sap_area="FI-AR",
        # CONFIRMED live (2026-09): get_accounts_receivable_summary, tested
        # against this tenant post-deploy, returned a real open item for
        # company code 1710 — the cube's Customer field IS modelled here after
        # all. Portfolio summary only, though: API_CUSTOMER_INVOICE_SRV
        # (single customer-invoice detail, dunning status) is still NOT
        # connected — see the API list handed to the connectivity team. A
        # specific "status of customer invoice X" question still has no live
        # source and falls back to the knowledge base / handoff.
        status="live",
        odata_services=("API_CUSTOMER_INVOICE_SRV", "API_OPLACCTGDOCITEMCUBE_SRV"),
        example_questions=(
            "What's our total accounts receivable for company code 1710?",
            "What is our dunning / collections process?",
            "When do we write off a bad debt?",
            "What are standard customer payment terms?",
            "What's the status of customer invoice 9400001234?",
        ),
        kb_docs=("accounts_receivable.md",),
    ),
    FinanceDomain(
        key="cost_centers",
        name="Cost Centers (CO-CCA)",
        sap_area="CO-CCA",
        status="live",
        odata_services=("API_COSTCENTER_SRV",),
        example_questions=(
            "How do I request a new cost center?",
            "Who is responsible for cost centre 1000?",
            "Is cost centre 1000 still valid, and what profit centre is it assigned to?",
            "How much has been spent on cost centre 1000 this fiscal year?",
        ),
        kb_docs=("cost_and_profit_centers.md",),
    ),
    FinanceDomain(
        key="profit_centers",
        name="Profit Centers (CO-PCA)",
        sap_area="CO-PCA",
        status="live",
        odata_services=("API_PROFITCENTER_SRV",),
        example_questions=(
            "How do I request a new profit center?",
            "What's the difference between a cost center and a profit center here?",
            "Is profit centre 1000 still valid, and what's its description?",
        ),
        kb_docs=("cost_and_profit_centers.md",),
    ),
    FinanceDomain(
        key="budgeting",
        name="Budgeting & Availability Control (FM / CO)",
        sap_area="FM / CO-OM",
        status="kb_only",
        # No budget/plan OData service is reliably active on this landscape,
        # so get_budget_status always returns budgetAvailable=false + a report
        # pointer — that's why this stays kb_only. It DOES compute real
        # actualSpend (from journal_entry_item, via API_OPLACCTGDOCITEMCUBE_SRV
        # / API_JOURNALENTRYITEMBASIC_SRV) and, for a PO, commitmentValue (from
        # API_PURCHASEORDER_PROCESS_SRV) — the "actual"/"committed" half of
        # these questions, never the budget/plan figure itself.
        odata_services=("API_BUDGET_ENTRY_DOCUMENT_SRV",),
        example_questions=(
            "Is there budget left for purchase order 4500001234?",
            "What's the plan vs actual for cost centre 1000?",
            "Is internal order 700123 over budget?",
            "How much has been spent on cost centre 1000 this fiscal year?",
        ),
        kb_docs=("cost_and_profit_centers.md",),
    ),
    FinanceDomain(
        key="fixed_assets",
        name="Fixed Assets (FI-AA)",
        sap_area="FI-AA",
        status="kb_only",
        odata_services=("API_FIXEDASSET_SRV",),
        example_questions=(
            "What's the capitalization threshold for a fixed asset?",
            "How is depreciation calculated / what useful life do we use?",
            "How do I retire or transfer an asset?",
            "What is an asset under construction (AUC) and how is it settled?",
        ),
        kb_docs=("fixed_assets.md",),
    ),
    FinanceDomain(
        key="bank_cash",
        name="Bank & Cash Management (TR-CM)",
        sap_area="FIN-FSCM-TRM / TR-CM",
        status="kb_only",
        odata_services=("API_BANK_STATEMENT_SRV", "API_BANKSTATEMENT_SRV", "API_HOUSEBANK_SRV"),
        example_questions=(
            "How and when are bank statements imported and reconciled?",
            "What's today's cash position?",
            "How do I add a new house bank account?",
        ),
        kb_docs=("bank_and_cash.md",),
    ),
    FinanceDomain(
        key="tax",
        name="Tax",
        sap_area="FI-AP/AR (tax)",
        status="kb_only",
        odata_services=("API_TAXCODE_SRV",),
        example_questions=(
            "Which tax code do I use for a domestic services purchase?",
            "When are VAT / sales tax returns filed?",
            "How is withholding tax handled for a foreign vendor?",
            "Where do I store a tax exemption certificate?",
        ),
        kb_docs=("tax.md",),
    ),
)

DOMAINS_BY_KEY = {d.key: d for d in FINANCE_DOMAINS}


def coverage_summary() -> dict:
    """Compact status roll-up for ``/ready`` and the use-case doc."""
    by_status: dict[str, list[str]] = {}
    for d in FINANCE_DOMAINS:
        by_status.setdefault(d.status, []).append(d.key)
    return {
        "total": len(FINANCE_DOMAINS),
        "live": by_status.get("live", []),
        "kb_only": by_status.get("kb_only", []),
        "planned": by_status.get("planned", []),
    }
