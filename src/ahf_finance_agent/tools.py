"""Tool definitions and dispatch for the agent's lookups.

:data:`TOOL_SPECS` is the OpenAI-style ``tools`` array handed to GPT 5.2 on
every answering turn. It carries two families:

* Build step 3 (+ later widening) — the read-only S/4HANA lookups.
  :func:`dispatch_tool` runs one against an
  :class:`~ahf_finance_agent.s4hana.S4HANAClient`.
* Build step 4 — ``search_policy_docs``, the RAG grounding tool.
  :func:`dispatch_policy_tool` runs one against the policy
  :class:`~ahf_finance_agent.knowledge_base.VectorIndex`.

Both return a :class:`ToolOutcome` — a JSON-serialisable payload plus a
``grounded`` flag the answering loop uses to mark the turn as data-backed.

Failures never raise out of here: a missing argument, bad JSON, an unknown
tool, an S/4HANA error, or a knowledge-base error all come back as
``{"error": …}`` / ``{"grounded": false, …}`` so the model can ask a clarifying
question or hand off, rather than the turn crashing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from ahf_finance_agent.config import get_settings
from ahf_finance_agent.s4hana import S4HANAClient, S4HANAError

logger = logging.getLogger(__name__)

# Cap on the size of a single tool result fed back to the model, in characters.
MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class ToolOutcome:
    content: Any            # JSON-serialisable — goes back to the model verbatim
    grounded: bool          # True when a real S/4HANA record was returned


class _MissingArg(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _req(args: dict, key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise _MissingArg(key)
    return str(value).strip()


def _record(rec: dict | None) -> ToolOutcome:
    if rec is None:
        return ToolOutcome({"found": False, "message": "No matching record found in S/4HANA."}, grounded=False)
    return ToolOutcome({"found": True, "record": rec}, grounded=True)


def _records(rows: list[dict]) -> ToolOutcome:
    return ToolOutcome({"count": len(rows), "records": rows}, grounded=bool(rows))


# --- handlers ---------------------------------------------------------
# Each maps validated tool arguments onto an S4HANAClient method.

def _get_invoice_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_invoice_status(_req(a, "invoice"), a.get("fiscal_year") or None))


def _get_customer_invoice_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_customer_invoice_status(_req(a, "customer_invoice"), a.get("fiscal_year") or None))


def _int_arg(a: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = a.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def _bool_arg(a: dict, key: str) -> bool | None:
    """Optional boolean tool argument — ``None`` when the model omitted it
    (as opposed to explicitly passing false), so callers can tell "not asked
    about" apart from "asked and false"."""
    raw = a.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes")


def _search_invoices_by_vendor(c: S4HANAClient, a: dict) -> ToolOutcome:
    limit = _int_arg(a, "limit", 10, 1, 50)
    skip = _int_arg(a, "skip", 0, 0, 10000)
    rows = c.search_invoices_by_vendor(
        _req(a, "vendor"), a.get("company_code") or None, top=limit, skip=skip
    )
    outcome = _records(rows)
    # Tell the model where the page sits so it can answer "give me N more".
    outcome.content["skip"] = skip
    outcome.content["returned"] = len(rows)
    outcome.content["next_skip"] = skip + len(rows)
    return outcome


def _get_payment_clearing_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(
        c.get_payment_clearing_status(
            _req(a, "accounting_document"), _req(a, "fiscal_year"), _req(a, "company_code")
        )
    )


def _get_invoice_payment_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_invoice_payment_status(_req(a, "invoice"), a.get("fiscal_year") or None))


def _get_purchase_order_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_order_status(_req(a, "purchase_order")))


def _get_purchase_order_items(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _records(c.get_purchase_order_items(_req(a, "purchase_order")))


def _get_purchase_order_delivery_schedule(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_order_delivery_schedule(_req(a, "purchase_order")))


def _check_three_way_match(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.check_three_way_match(_req(a, "invoice"), _req(a, "fiscal_year")))


def _get_purchase_order_approval_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_order_approval_status(_req(a, "purchase_order")))


def _get_goods_receipts_for_po(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_goods_receipts_for_po(_req(a, "purchase_order")))


def _get_invoice_items(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _records(c.get_invoice_items(_req(a, "invoice"), _req(a, "fiscal_year")))


def _get_purchase_requisition_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_requisition_status(_req(a, "purchase_requisition")))


def _get_vendor_details(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_vendor_details(_req(a, "business_partner")))


def _get_vendor_email_addresses(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _records(c.get_vendor_email_addresses(_req(a, "business_partner")) or [])


def _get_vendor_bank_accounts(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _records(c.get_vendor_bank_accounts(_req(a, "business_partner")) or [])


def _get_budget_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    # The budget/plan figure itself is never available (grounded stays False
    # for that), but actualSpend / commitmentValue are computed from live
    # postings when present — count the call as grounded when either landed.
    result = c.get_budget_status(_req(a, "cost_object_type"), _req(a, "cost_object_id"), a.get("fiscal_year") or None)
    grounded = bool(result.get("actualSpend") or result.get("commitmentValue"))
    return ToolOutcome(result, grounded=grounded)


def _get_company_code_details(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_company_code_details(_req(a, "company_code")))


def _list_companies_with_open_ap_ar_balance(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.list_companies_with_open_ap_ar_balance()
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_cost_center_details(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_cost_center_details(_req(a, "cost_center"), a.get("controlling_area") or None))


def _get_profit_center_details(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_profit_center_details(_req(a, "profit_center"), a.get("controlling_area") or None))


def _get_gl_account_master(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_gl_account_master(_req(a, "gl_account"), a.get("chart_of_accounts") or None))


def _get_gl_account_activity(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_gl_account_activity(_req(a, "company_code"), _req(a, "gl_account"), a.get("fiscal_year") or None))


def _get_accounts_payable_summary(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_accounts_payable_summary(
        _req(a, "company_code"), a.get("vendor") or None, a.get("fiscal_year") or None
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_accounts_receivable_summary(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_accounts_receivable_summary(
        _req(a, "company_code"), a.get("customer") or None, a.get("fiscal_year") or None
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


# -- AP open-items drill-down / analytics tools --------------------------

def _list_open_invoices_for_vendor(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.list_open_invoices_for_vendor(
        _req(a, "vendor"), a.get("company_code") or None, top=_int_arg(a, "top", 20, 1, 100),
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_largest_open_item(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_largest_open_item(_req(a, "company_code"), a.get("vendor") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_ap_aging_summary(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_ap_aging_summary(_req(a, "company_code"), a.get("vendor") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_top_vendors_by_open_payable(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_top_vendors_by_open_payable(_req(a, "company_code"), top=_int_arg(a, "top", 5, 1, 20))
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_average_days_to_clear(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_average_days_to_clear(a.get("vendor") or None, a.get("company_code") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


# -- AR open-items drill-down / analytics tools --------------------------

def _list_open_invoices_for_customer(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.list_open_invoices_for_customer(
        _req(a, "customer"), a.get("company_code") or None, top=_int_arg(a, "top", 20, 1, 100),
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_largest_open_receivable(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_largest_open_receivable(_req(a, "company_code"), a.get("customer") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_ar_aging_summary(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_ar_aging_summary(_req(a, "company_code"), a.get("customer") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_top_customers_by_open_receivable(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_top_customers_by_open_receivable(_req(a, "company_code"), top=_int_arg(a, "top", 5, 1, 20))
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _get_average_days_to_collect(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.get_average_days_to_collect(a.get("customer") or None, a.get("company_code") or None)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


# -- "how many" / volume-count tools ------------------------------------

def _count_purchase_orders(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_purchase_orders(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
        a.get("vendor") or None, _bool_arg(a, "pending_approval"), a.get("company_code") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_purchase_requisitions(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_purchase_requisitions(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_supplier_invoices(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_supplier_invoices(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
        _bool_arg(a, "blocked_for_payment"), a.get("vendor") or None, a.get("company_code") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_invoices_by_fiscal_period(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_invoices_by_fiscal_period(
        _req(a, "company_code"), _req(a, "fiscal_year"), _req(a, "fiscal_period"),
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_goods_receipts(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_goods_receipts(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
        a.get("purchase_order") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_pos_overdue_without_goods_receipt(c: S4HANAClient, a: dict) -> ToolOutcome:
    cap = _int_arg(a, "cap_purchase_orders", 30, 1, 100)
    result = c.count_pos_overdue_without_goods_receipt(cap)
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_cleared_documents(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_cleared_documents(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
        a.get("company_code") or None, a.get("vendor") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_new_vendors(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_new_vendors(
        a.get("period") or None, a.get("date_from") or None, a.get("date_to") or None,
    )
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _count_blocked_vendors(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.count_blocked_vendors()
    return ToolOutcome(result, grounded=bool(result.get("connected")))


def _search_vendors_by_name(c: S4HANAClient, a: dict) -> ToolOutcome:
    result = c.search_vendors_by_name(_req(a, "name"))
    return ToolOutcome(result, grounded=bool(result.get("connected")) and bool(result.get("matches")))


_HANDLERS: dict[str, Callable[[S4HANAClient, dict], ToolOutcome]] = {
    "get_invoice_status": _get_invoice_status,
    "get_customer_invoice_status": _get_customer_invoice_status,
    "get_invoice_items": _get_invoice_items,
    "search_invoices_by_vendor": _search_invoices_by_vendor,
    "get_payment_clearing_status": _get_payment_clearing_status,
    "get_invoice_payment_status": _get_invoice_payment_status,
    "get_purchase_order_status": _get_purchase_order_status,
    "get_purchase_order_items": _get_purchase_order_items,
    "get_purchase_order_delivery_schedule": _get_purchase_order_delivery_schedule,
    "get_purchase_order_approval_status": _get_purchase_order_approval_status,
    "check_three_way_match": _check_three_way_match,
    "get_goods_receipts_for_po": _get_goods_receipts_for_po,
    "get_purchase_requisition_status": _get_purchase_requisition_status,
    "get_vendor_details": _get_vendor_details,
    "get_vendor_email_addresses": _get_vendor_email_addresses,
    "get_vendor_bank_accounts": _get_vendor_bank_accounts,
    "get_budget_status": _get_budget_status,
    "get_company_code_details": _get_company_code_details,
    "list_companies_with_open_ap_ar_balance": _list_companies_with_open_ap_ar_balance,
    "get_cost_center_details": _get_cost_center_details,
    "get_profit_center_details": _get_profit_center_details,
    "get_gl_account_master": _get_gl_account_master,
    "get_gl_account_activity": _get_gl_account_activity,
    "get_accounts_payable_summary": _get_accounts_payable_summary,
    "get_accounts_receivable_summary": _get_accounts_receivable_summary,
    "list_open_invoices_for_vendor": _list_open_invoices_for_vendor,
    "get_largest_open_item": _get_largest_open_item,
    "get_ap_aging_summary": _get_ap_aging_summary,
    "get_top_vendors_by_open_payable": _get_top_vendors_by_open_payable,
    "get_average_days_to_clear": _get_average_days_to_clear,
    "list_open_invoices_for_customer": _list_open_invoices_for_customer,
    "get_largest_open_receivable": _get_largest_open_receivable,
    "get_ar_aging_summary": _get_ar_aging_summary,
    "get_top_customers_by_open_receivable": _get_top_customers_by_open_receivable,
    "get_average_days_to_collect": _get_average_days_to_collect,
    "count_purchase_orders": _count_purchase_orders,
    "count_purchase_requisitions": _count_purchase_requisitions,
    "count_supplier_invoices": _count_supplier_invoices,
    "count_invoices_by_fiscal_period": _count_invoices_by_fiscal_period,
    "count_goods_receipts": _count_goods_receipts,
    "count_pos_overdue_without_goods_receipt": _count_pos_overdue_without_goods_receipt,
    "count_cleared_documents": _count_cleared_documents,
    "count_new_vendors": _count_new_vendors,
    "count_blocked_vendors": _count_blocked_vendors,
    "search_vendors_by_name": _search_vendors_by_name,
}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict] = [
    _fn(
        "get_invoice_status",
        "Look up the status and header of a SAP S/4HANA supplier invoice by its "
        "invoice number (payment terms, blocking reason, posting date, amount). "
        "The invoice number is unique on its own — call this with just the number "
        "and do NOT ask the user for a fiscal year. Use for 'is invoice X paid / "
        "blocked / posted'. SupplierInvoiceStatus is a raw internal SAP code with "
        "no confirmed label mapping on this tenant — never quote it to the user or "
        "guess a status word from it. Say 'posted' only because AccountingDocument "
        "is present, and blocked/not blocked from PaymentBlockingReason.",
        {
            "invoice": {"type": "string", "description": "Supplier invoice number, e.g. 5105601234"},
            "fiscal_year": {
                "type": "string",
                "description": "Optional 4-digit fiscal year; only pass it if the user volunteered one.",
            },
        },
        ["invoice"],
    ),
    _fn(
        "get_customer_invoice_status",
        "Look up the status and header of a SAP S/4HANA CUSTOMER (AR) invoice by "
        "its invoice number — the accounts-receivable mirror of "
        "get_invoice_status. Use for 'status of customer invoice X', 'has "
        "customer invoice X been posted / paid'. Newly connected (2026-09): "
        "unlike get_accounts_receivable_summary's confirmed fields, no live "
        "customer invoice number has verified this path yet, so relay a "
        "not-found result plainly and hand off to AP/AR support rather than "
        "asserting the invoice doesn't exist. The invoice number is unique on "
        "its own — call with just the number and only pass a fiscal year if the "
        "user volunteered one. For the portfolio-level 'how much are we owed' "
        "question use get_accounts_receivable_summary instead.",
        {
            "customer_invoice": {"type": "string", "description": "Customer invoice number, e.g. 9400001234"},
            "fiscal_year": {
                "type": "string",
                "description": "Optional 4-digit fiscal year; only pass it if the user volunteered one.",
            },
        },
        ["customer_invoice"],
    ),
    _fn(
        "search_invoices_by_vendor",
        "List recent supplier invoices and their status for a given vendor "
        "(invoicing party) in SAP S/4HANA, most recent first. Supports paging: "
        "for a follow-up like 'give me 10 more' or 'next page', call again with "
        "`skip` set to the `next_skip` value from the previous result.",
        {
            "vendor": {"type": "string", "description": "Vendor / invoicing party number, e.g. USSU-VSF04"},
            "company_code": {"type": "string", "description": "Optional company code to narrow the search, e.g. 1710"},
            "limit": {"type": "integer", "description": "How many to return this page (1-50, default 10)."},
            "skip": {"type": "integer", "description": "How many to skip (for paging). Use the previous result's next_skip."},
        },
        ["vendor"],
    ),
    _fn(
        "get_payment_clearing_status",
        "Check whether a payment has actually posted (cleared) against an accounting "
        "document in SAP S/4HANA, as opposed to the invoice merely being posted or "
        "blocked. Returns isCleared, the clearing date, and — when cleared — a "
        "paymentSummary (payment document, method, house bank, amount). For an "
        "invoice number rather than an FI document, use get_invoice_payment_status.",
        {
            "accounting_document": {"type": "string", "description": "Accounting document number, e.g. 1400000123"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year, e.g. 2026"},
            "company_code": {"type": "string", "description": "Company code, e.g. 1000"},
        },
        ["accounting_document", "fiscal_year", "company_code"],
    ),
    _fn(
        "get_invoice_payment_status",
        "Given a SAP S/4HANA supplier invoice number, determine whether payment "
        "has been sent (cleared): this resolves the invoice's FI accounting "
        "document (the invoice header often does NOT return it) and then runs the "
        "clearing check for you. Returns isBlockedForPayment / paymentBlockingReason, "
        "paymentCleared, clearingDate, accountingDocumentSource, and — when paid — a "
        "paymentSummary with the payment document, date, method, house bank and "
        "amount. Use for 'has invoice X been paid', 'was payment sent for invoice X', "
        "'what accounting document was created for invoice X', 'how / when was invoice "
        "X paid', 'what payment run was invoice X in' (give the paymentSummary — the "
        "payment document + date + bank identify the run; the literal F110 run ID is "
        "not in any API, so add that AP Payments can pin the exact run from those "
        "details). Call with just the invoice number; only pass a fiscal year if the "
        "user volunteered one.",
        {
            "invoice": {"type": "string", "description": "Supplier invoice number, e.g. 5105601234"},
            "fiscal_year": {
                "type": "string",
                "description": "Optional 4-digit fiscal year; only pass it if the user volunteered one.",
            },
        },
        ["invoice"],
    ),
    _fn(
        "get_purchase_order_status",
        "Look up a purchase order's header, release/approval state, and processing "
        "status in SAP S/4HANA by PO number.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
    ),
    _fn(
        "get_purchase_order_items",
        "List a SAP S/4HANA purchase order's line items: ordered quantity, unit, "
        "net price and price unit, per PO item. Use when you need the ordered "
        "quantity or PO price for a specific item, e.g. to compare against an "
        "invoice or goods receipt.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
    ),
    _fn(
        "get_purchase_order_delivery_schedule",
        "Get the DELIVERY DATE(S) for a SAP S/4HANA purchase order. The PO header "
        "and line items carry no delivery date — it lives on the schedule lines. "
        "Returns earliestDeliveryDate / latestDeliveryDate and a line per PO item "
        "/ schedule line (delivery date, scheduled quantity). Use for 'when is PO "
        "X due', 'what's the delivery date on PO X', 'when is the delivery "
        "scheduled'. Returns found=false or an error if this system does not "
        "expose PO schedule lines.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
    ),
    _fn(
        "check_three_way_match",
        "Assess whether a SAP S/4HANA supplier invoice matches its purchase order "
        "and goods receipt (three-way match), and whether there is a quantity or "
        "price variance. Needs BOTH the invoice number and its fiscal year — take "
        "the fiscal year from a prior get_invoice_status result and do NOT ask the "
        "user for it. Returns the invoice's payment block (paymentBlockingReason / "
        "isBlockedForPayment — the authoritative signal that SAP's own invoice "
        "verification blocked it on a variance) plus a per-line computed comparison "
        "of invoiced vs PO vs goods-receipt quantity and price. Use for 'did "
        "invoice X pass the 3-way match', 'is there a quantity/price variance on "
        "invoice X', 'why is invoice X blocked'. Tolerance limits are policy — use "
        "search_policy_docs for those.",
        {
            "invoice": {"type": "string", "description": "Supplier invoice number, e.g. 5105601234"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year of the invoice, e.g. 2026"},
        },
        ["invoice", "fiscal_year"],
    ),
    _fn(
        "get_purchase_order_approval_status",
        "Check specifically whether a SAP S/4HANA purchase order has been RELEASED "
        "/ APPROVED via its release strategy, and whether any release step is "
        "still outstanding. Returns an approvalSummary (isReleased, "
        "releaseIncomplete). Use for 'has PO X been approved / released', 'is PO "
        "X still waiting for sign-off'.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
    ),
    _fn(
        "get_goods_receipts_for_po",
        "List the goods receipts (inbound material documents) posted against a SAP "
        "S/4HANA purchase order: movement type, quantity, plant, posting date, and "
        "whether a receipt was later cancelled. Use for 'has PO X been received', "
        "'what goods receipts exist for PO X', 'was the delivery for PO X booked'.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
    ),
    _fn(
        "get_invoice_items",
        "List the purchase-order-referenced line items on a SAP S/4HANA supplier "
        "invoice (PO + item, quantity, amount, tax code). Needs BOTH the invoice "
        "number and its fiscal year: take the fiscal year from a prior "
        "get_invoice_status result and do NOT ask the user for it. Use for 'what "
        "is invoice X made up of', 'which PO does invoice X bill against'.",
        {
            "invoice": {"type": "string", "description": "Supplier invoice number, e.g. 5105601234"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year of the invoice, e.g. 2026"},
        },
        ["invoice", "fiscal_year"],
    ),
    _fn(
        "get_purchase_requisition_status",
        "Look up a purchase requisition's header and per-item processing / release "
        "status in SAP S/4HANA by PR number.",
        {"purchase_requisition": {"type": "string", "description": "Purchase requisition number, e.g. 1000005678"}},
        ["purchase_requisition"],
    ),
    _fn(
        "get_vendor_details",
        "Look up a vendor / business partner's master-data setup and block status in "
        "SAP S/4HANA to confirm onboarding. Never returns bank, IBAN, tax id, or "
        "personal contact details.",
        {"business_partner": {"type": "string", "description": "Business partner / vendor number, e.g. 100000"}},
        ["business_partner"],
    ),
    _fn(
        "get_vendor_email_addresses",
        "Look up the email address(es) on file for a vendor / business partner in "
        "SAP S/4HANA: the address's own company-level mailbox (contactPerson blank, "
        "e.g. an 'info@' address) and any named contact person's email "
        "(contactPerson set to that person's own business-partner number). This is "
        "the ONE tool in this agent allowed to return an email address — every "
        "other tool strips it. Use ONLY when the user explicitly asks for a vendor "
        "or contact email; do not volunteer it otherwise, and never return bank, "
        "IBAN, tax id, or phone/address details even from this tool's raw result.",
        {"business_partner": {"type": "string", "description": "Business partner / vendor number, e.g. 100000"}},
        ["business_partner"],
    ),
    _fn(
        "get_vendor_bank_accounts",
        "Look up the bank account / payment-routing details on file for a vendor / "
        "business partner in SAP S/4HANA: bank name, IBAN, bank key, account number, "
        "SWIFT/BIC, and account holder name. A vendor can have more than one bank on "
        "file. This is master data (where payments to this "
        "vendor are routed) — NOT a bank statement; there is no live bank-statement "
        "(transaction) source on this tenant, so treat 'bank statement' questions as "
        "not connected. This is the ONE tool allowed to return vendor bank/IBAN "
        "data — every other tool strips it. Use ONLY when the user explicitly asks "
        "for a vendor's bank account/IBAN/payment details; do not volunteer it "
        "otherwise. Never return a tax id or personal name/phone/home address even "
        "from this tool's raw result. Because vendor-bank-detail changes are a "
        "classic payment-redirection-fraud vector, if the user implies they are "
        "about to CHANGE or ACT on a vendor's bank details based on this, remind "
        "them to verify any change through a secure out-of-band process first — "
        "and that you cannot make the change yourself (read-only).",
        {"business_partner": {"type": "string", "description": "Business partner / vendor number, e.g. 100000"}},
        ["business_partner"],
    ),
    _fn(
        "get_budget_status",
        "Check the budget / availability-control status for a cost centre, internal "
        "order, or a purchase order's account assignment (e.g. 'is there budget for "
        "PO X', 'how much budget is left on cost centre Y', 'what's the plan vs "
        "actual for order Z', 'is order Z over budget'). On this landscape no budget "
        "OData service is connected, so budgetAvailable is ALWAYS false and the "
        "budget/plan/remaining figure itself is never available — never state or "
        "estimate one. It DOES compute and return real data when present: "
        "actualSpend (net posted actuals against the cost object, from live journal "
        "entries — the 'actual' half of 'plan vs actual') and, for a purchase order, "
        "commitmentValue (the PO's own committed line-item value). Relay these as "
        "real figures when non-null, and always relay the handoff (which S/4HANA "
        "report / team has the budget/plan side).",
        {
            "cost_object_type": {
                "type": "string",
                "enum": ["cost_center", "internal_order", "purchase_order"],
                "description": "What the budget is held against.",
            },
            "cost_object_id": {
                "type": "string",
                "description": "The cost centre / internal order / purchase order number.",
            },
            "fiscal_year": {"type": "string", "description": "Optional 4-digit fiscal year."},
        },
        ["cost_object_type", "cost_object_id"],
    ),
    _fn(
        "get_company_code_details",
        "Look up a SAP S/4HANA company code's master data: name, country, city, "
        "currency, chart of accounts, fiscal year variant. Use for 'what currency "
        "is company code X in', 'what chart of accounts does CC X use', 'what "
        "fiscal year variant is company code X on'.",
        {"company_code": {"type": "string", "description": "Company code, e.g. 1710"}},
        ["company_code"],
    ),
    _fn(
        "list_companies_with_open_ap_ar_balance",
        "The ONLY tool here that does not need a company code up front — use it "
        "for 'which companies have an open AP/AR balance', 'list all company "
        "codes with outstanding payables or receivables', or as the first step "
        "when the user wants a cross-company view instead of naming one company "
        "code. Scans a capped set of company codes and, for each, computes the "
        "SAME open AP/AR figures get_accounts_payable_summary / "
        "get_accounts_receivable_summary would report (identical cube, cap, and "
        "caveats — NOT an official aging report, directional only). Returns "
        "ONLY company codes with at least one open AP or AR item, each flagged "
        "hasApBalance / hasArBalance so you can further filter to 'both' "
        "yourself if asked. Deliberately checks only a SMALL sample of company "
        "codes (each one is several sequential S/4HANA calls, so this is slower "
        "than the other tools here) — always relay companyCodesScanned / the "
        "capped note so the user knows this is a sample, not a complete list. A "
        "company code not in the result either has no open items or this "
        "tenant's AR field support is unconfirmed for it — relay the note "
        "verbatim rather than asserting a confirmed zero for AR. Do NOT "
        "follow this up by calling get_accounts_payable_summary / "
        "get_accounts_receivable_summary again for a company code already "
        "in this result — apOpenItemCount/apNetOpenAmount/arOpenItemCount/"
        "arNetOpenAmount here ARE that exact figure, already computed; "
        "re-calling those tools per company only adds latency for no new "
        "data (each is several more sequential S/4HANA round trips, and "
        "this whole answer is already time-boxed against the caller's own "
        "timeout).",
        {},
        [],
    ),
    _fn(
        "get_cost_center_details",
        "Look up a SAP S/4HANA cost centre's MASTER DATA: validity period, "
        "responsible person, category, assigned profit centre and company code. "
        "Returns isCurrentlyValid (true/false/null) — use that directly for 'is "
        "cost centre X still active/valid', don't compute it yourself from the "
        "raw validity dates. This is who owns it / is it valid — for live spend "
        "against it, use get_budget_status or the actual-spend figure it returns "
        "instead. Use for 'who is responsible for cost centre X', 'what profit "
        "centre is cost centre X assigned to'.",
        {
            "cost_center": {"type": "string", "description": "Cost centre number, e.g. 1000"},
            "controlling_area": {"type": "string", "description": "Optional controlling area, if the user gives one."},
        },
        ["cost_center"],
    ),
    _fn(
        "get_profit_center_details",
        "Look up a SAP S/4HANA profit centre's master data: responsible person, "
        "segment, block status, validity period. Returns isCurrentlyValid "
        "(true/false/null, already accounting for both the block flag and the "
        "validity dates) — use that directly for 'is profit centre X still "
        "valid', don't compute it yourself from the raw fields. Use for 'what is "
        "profit centre X', 'who is responsible for profit centre X'.",
        {
            "profit_center": {"type": "string", "description": "Profit centre number, e.g. 1000"},
            "controlling_area": {"type": "string", "description": "Optional controlling area, if the user gives one."},
        },
        ["profit_center"],
    ),
    _fn(
        "get_gl_account_master",
        "Look up a SAP S/4HANA G/L account's MASTER DATA: account group, whether "
        "it's a balance-sheet or P&L account, posting/planning block status, short "
        "description. This is what the account IS, not its balance — for postings, "
        "use get_gl_account_activity. Use for 'what is G/L account X', 'is G/L "
        "account X a balance sheet or P&L account'.",
        {
            "gl_account": {"type": "string", "description": "G/L account number, e.g. 400000"},
            "chart_of_accounts": {"type": "string", "description": "Optional chart of accounts, if the user gives one."},
        },
        ["gl_account"],
    ),
    _fn(
        "get_gl_account_activity",
        "Net posted amount on a G/L account in a company code, computed from live "
        "journal-entry line items (debits minus credits). This is a COMPUTED sum of "
        "postings in scope, NOT an official trial-balance / period-end account "
        "balance (no carry-forward). For 'what is the balance on G/L account X', "
        "give this figure but be clear it's a computed posting total, not an "
        "official balance, and point to the G/L balance report for that. Needs "
        "both company_code and gl_account.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "gl_account": {"type": "string", "description": "G/L account number, e.g. 400000"},
            "fiscal_year": {"type": "string", "description": "Optional 4-digit fiscal year."},
        },
        ["company_code", "gl_account"],
    ),
    _fn(
        "get_accounts_payable_summary",
        "PORTFOLIO-level AP question — use this for 'how much do we owe', 'what's "
        "our total accounts payable', 'how many open vendor invoices are there', "
        "'how many invoices are overdue for payment' for a company code (optionally "
        "one vendor), as opposed to every other AP tool here which needs one "
        "specific invoice number. Computes open (not yet cleared) vendor-subledger "
        "postings from live data: openItemCount and netOpenAmount (positive = "
        "amount owed), plus overdueCount/overdueAmount (the subset already past "
        "NetDueDate). NOT an official AP aging report — no day-based aging buckets, "
        "capped at the most recent ~200 postings scanned (under-counts if there are "
        "more — the note says when it was truncated). Always relay the note "
        "verbatim and point to the AP aging report / FBL1N for a definitive figure "
        "— treat this as directional, not final.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "vendor": {"type": "string", "description": "Optional supplier/vendor number to scope to one vendor."},
            "fiscal_year": {"type": "string", "description": "Optional 4-digit fiscal year."},
        },
        ["company_code"],
    ),
    _fn(
        "get_accounts_receivable_summary",
        "PORTFOLIO-level AR question — use this for 'how much are we owed', "
        "'what's our total accounts receivable / outstanding balance', 'how many "
        "open customer invoices are there' for a company code (optionally one "
        "customer). Mirrors get_accounts_payable_summary but for the customer "
        "subledger, and may report connected=false: this tenant's live AR field "
        "support is NOT confirmed the way AP is, so a not-connected result is "
        "expected and should be relayed honestly (fall back to search_policy_docs "
        "for AR policy/process and hand off for the live figure), not treated as a "
        "tool error. When connected=true, same caveats as the AP version apply: "
        "not an official aging report, capped at ~200 postings, treat as directional.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "customer": {"type": "string", "description": "Optional customer number to scope to one customer."},
            "fiscal_year": {"type": "string", "description": "Optional 4-digit fiscal year."},
        },
        ["company_code"],
    ),
    # -- AP open-items drill-down / analytics -------------------------
    # All five reuse the same live-confirmed open-items data
    # get_accounts_payable_summary uses (not a new field/service) — same
    # caveats apply: computed, capped, NOT an official AP aging report.
    _fn(
        "list_open_invoices_for_vendor",
        "List of open (unpaid) invoice-level accounting documents for a vendor "
        "— 'which invoices for vendor X are still unpaid'. Drills down from "
        "get_accounts_payable_summary's aggregate into the actual documents, "
        "oldest first, each flagged overdue true/false/null. NOT an official "
        "AP aging report.",
        {
            "vendor": {"type": "string", "description": "Supplier/vendor number."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
            "top": {"type": "integer", "description": "Max invoices to return (default 20, max 100)."},
        },
        ["vendor"],
    ),
    _fn(
        "get_largest_open_item",
        "The single largest open (unpaid) vendor invoice for a company code, "
        "optionally scoped to one vendor — 'what's our largest unpaid invoice'.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "vendor": {"type": "string", "description": "Optional supplier/vendor number."},
        },
        ["company_code"],
    ),
    _fn(
        "get_ap_aging_summary",
        "Rough AP aging breakdown (current / 1-30 / 31-60 / 60+ days overdue) "
        "for open vendor items in a company code, optionally one vendor — "
        "'break down our open payables by aging bucket'. NOT the official AP "
        "aging report (FBL1N) — always relay that caveat.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "vendor": {"type": "string", "description": "Optional supplier/vendor number."},
        },
        ["company_code"],
    ),
    _fn(
        "get_top_vendors_by_open_payable",
        "Top vendors by total open (unpaid) amount for a company code — 'who "
        "are our top vendors by amount owed'.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "top": {"type": "integer", "description": "How many vendors to return (default 5, max 20)."},
        },
        ["company_code"],
    ),
    _fn(
        "get_average_days_to_clear",
        "Average days from posting to clearing for a vendor's (or company "
        "code's) cleared invoices — 'on average how long does it take us to "
        "pay vendor X'. A rough payment-cycle-time indicator, not an official "
        "metric. Best scoped by vendor and/or company_code.",
        {
            "vendor": {"type": "string", "description": "Optional supplier/vendor number."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
        },
        [],
    ),
    # -- AR open-items drill-down / analytics -------------------------
    # Customer-side mirror of the AP family above. Same live-confirmed
    # open-items data get_accounts_receivable_summary uses (not a new
    # field/service) — same caveats apply: computed, capped, NOT an
    # official AR aging report.
    _fn(
        "list_open_invoices_for_customer",
        "List of open (unpaid) invoice-level accounting documents for a "
        "customer — 'which invoices for customer X are still unpaid'. Drills "
        "down from get_accounts_receivable_summary's aggregate into the "
        "actual documents, oldest first, each flagged overdue true/false/"
        "null. NOT an official AR aging report.",
        {
            "customer": {"type": "string", "description": "Customer number."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
            "top": {"type": "integer", "description": "Max invoices to return (default 20, max 100)."},
        },
        ["customer"],
    ),
    _fn(
        "get_largest_open_receivable",
        "The single largest open (unpaid) customer invoice for a company "
        "code, optionally scoped to one customer — 'what's our largest "
        "unpaid receivable'.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "customer": {"type": "string", "description": "Optional customer number."},
        },
        ["company_code"],
    ),
    _fn(
        "get_ar_aging_summary",
        "Rough AR aging breakdown (current / 1-30 / 31-60 / 60+ days overdue) "
        "for open customer items in a company code, optionally one customer "
        "— 'break down our open receivables by aging bucket'. NOT the "
        "official AR aging report (FBL5N) — always relay that caveat.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "customer": {"type": "string", "description": "Optional customer number."},
        },
        ["company_code"],
    ),
    _fn(
        "get_top_customers_by_open_receivable",
        "Top customers by total open (unpaid) amount for a company code — "
        "'who owes us the most'.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "top": {"type": "integer", "description": "How many customers to return (default 5, max 20)."},
        },
        ["company_code"],
    ),
    _fn(
        "get_average_days_to_collect",
        "Average days from posting to clearing for a customer's (or company "
        "code's) cleared invoices — 'on average how long does it take "
        "customer X to pay us'. A rough collection-cycle-time indicator, not "
        "an official metric. Best scoped by customer and/or company_code.",
        {
            "customer": {"type": "string", "description": "Optional customer number."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
        },
        [],
    ),
    # -- "how many" / volume-count tools ------------------------------
    # Shared conventions across all of these: {count, capped, connected,
    # filters, note} on success ({count: null, connected: false, message} on
    # a lookup failure) — always relay capped/note verbatim ("at least N",
    # never a confident "exactly N" once capped=true). `period` is a
    # shorthand ("this_week" etc); pass explicit date_from/date_to
    # (YYYY-MM-DD) instead for anything else the user names. Every date-range
    # and boolean filter behind these is a first use on this tenant — a
    # connected=false result may mean the assumption needs a live fix, not
    # that the question has no answer. IMPORTANT: every one of these ALSO
    # returns a capped sample of the actual matching record numbers (key
    # named per tool, e.g. purchaseOrders/invoices/vendors — see each tool's
    # description below) alongside the count. If the user follows up with
    # 'list them' / 'which ones' / 'show me', answer from that sample
    # directly (or call the same tool again) — do NOT say listing isn't
    # possible; only say so if the sample list is genuinely empty.
    _fn(
        "count_purchase_orders",
        "Count of purchase orders matching a scope — 'how many POs were created "
        "last week', 'how many POs are pending approval right now', 'how many POs "
        "for vendor X this year'. vendor is a supplier NUMBER — if the user names a "
        "vendor, call search_vendors_by_name first to resolve it. pending_approval="
        "true filters to POs whose release is not yet complete. Response includes "
        "purchaseOrders: a sample of up to 20 matching PO numbers — use it directly "
        "if asked to list them.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on PO creation date. Omit if the user gives explicit dates or no date at all.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD), inclusive. Use instead of period for a range period doesn't cover."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD), inclusive."},
            "vendor": {"type": "string", "description": "Optional supplier/vendor NUMBER to scope to one vendor."},
            "pending_approval": {"type": "boolean", "description": "true = only POs whose release/approval is not yet complete."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
        },
        [],
    ),
    _fn(
        "count_purchase_requisitions",
        "Count of purchase requisitions created in a date range — 'how many PRs "
        "were raised this month'. On this tenant the PR service may report "
        "connected=false (a known, pre-existing authorisation gap, not specific to "
        "this count) — relay that plainly rather than implying zero. Response "
        "includes purchaseRequisitions: a sample of up to 20 matching PR numbers.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on PR creation date.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD)."},
        },
        [],
    ),
    _fn(
        "count_supplier_invoices",
        "Count of supplier invoices matching a scope — 'how many invoices were "
        "received last week', 'how many invoices are currently blocked for "
        "payment'. Date range applies to posting date. Do NOT use this for 'how "
        "many invoices were posted in fiscal period N' — use "
        "count_invoices_by_fiscal_period for that (this tool has no fiscal-period "
        "filter). Response includes invoices: a sample of up to 20 matching "
        "{supplierInvoice, fiscalYear} pairs.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on invoice posting date.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD)."},
            "blocked_for_payment": {"type": "boolean", "description": "true = only invoices with a payment block set."},
            "vendor": {"type": "string", "description": "Optional supplier/vendor number to scope to one vendor."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
        },
        [],
    ),
    _fn(
        "count_invoices_by_fiscal_period",
        "Count of vendor invoices posted in a specific fiscal period — 'how many "
        "invoices were posted in fiscal period 5 for company code 1710, fiscal "
        "year 2017'. Needs all three of company_code, fiscal_year, fiscal_period "
        "(no date-range shorthand here — fiscal periods aren't calendar months). "
        "Response includes accountingDocuments: a sample of up to 20 matching "
        "accounting document numbers.",
        {
            "company_code": {"type": "string", "description": "Company code, e.g. 1710"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year, e.g. 2017"},
            "fiscal_period": {"type": "string", "description": "Fiscal period number, e.g. 5"},
        },
        ["company_code", "fiscal_year", "fiscal_period"],
    ),
    _fn(
        "count_goods_receipts",
        "Count of distinct goods-receipt documents posted in a date range, "
        "optionally for one PO — 'how many goods receipts were posted this week'. "
        "Does NOT filter to receipts against still-open POs — it counts every "
        "matching (non-cancelled) receipt regardless of the PO's completion "
        "status; say so if the user's question implied that scoping. Response "
        "includes materialDocuments: a sample of up to 20 matching "
        "{materialDocument, materialDocumentYear} pairs.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on goods-receipt posting date.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD)."},
            "purchase_order": {"type": "string", "description": "Optional PO number to scope to one PO."},
        },
        [],
    ),
    _fn(
        "count_pos_overdue_without_goods_receipt",
        "Best-effort, EXPLICITLY SAMPLED (not exhaustive) count of purchase orders "
        "with an overdue delivery schedule line and no goods receipt yet — 'how "
        "many POs are overdue with no goods receipt'. Slower than the other count "
        "tools (checks each sampled PO individually) and always returns a "
        "sample-based figure — relay scannedPurchaseOrders and the note verbatim, "
        "phrase the answer as 'at least N among the first M overdue POs checked', "
        "never a confident total. Response includes purchaseOrders: the actual PO "
        "numbers found without a goods receipt (not just a count) — use it "
        "directly if asked to list them.",
        {
            "cap_purchase_orders": {
                "type": "integer",
                "description": "How many distinct overdue POs to sample (default 30, max 100). Higher is slower.",
            },
        },
        [],
    ),
    _fn(
        "count_cleared_documents",
        "Count of distinct accounting documents cleared in a date range — 'how "
        "many accounting documents were cleared last week', or scoped to one "
        "vendor, 'how many invoices were paid to vendor X this month' (pass "
        "vendor=<supplier number>). Date range applies to the clearing date. "
        "Response includes accountingDocuments: a sample of up to 20 matching "
        "accounting document numbers.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on clearing date.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD)."},
            "company_code": {"type": "string", "description": "Optional company code, e.g. 1710."},
            "vendor": {"type": "string", "description": "Optional supplier/vendor number — use for 'paid to vendor X'."},
        },
        [],
    ),
    _fn(
        "count_new_vendors",
        "Count of vendors created in a date range — 'how many new vendors were "
        "onboarded this quarter'. Response includes vendors: a sample of up to 20 "
        "matching {supplier, supplierName} pairs.",
        {
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "last_week", "this_month", "last_month", "this_quarter", "this_year"],
                "description": "Shorthand date range on vendor creation date.",
            },
            "date_from": {"type": "string", "description": "Explicit start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Explicit end date (YYYY-MM-DD)."},
        },
        [],
    ),
    _fn(
        "count_blocked_vendors",
        "Count of vendors currently blocked for posting or purchasing — 'how many "
        "vendors are blocked right now'. No arguments. Response includes vendors: "
        "a sample of up to 20 matching {supplier, supplierName} pairs.",
        {},
        [],
    ),
    _fn(
        "search_vendors_by_name",
        "Resolve a vendor/supplier NAME to its supplier number(s) — every other "
        "tool here needs a NUMBER, not a name. Use this first whenever the user "
        "names a vendor by name instead of by number (e.g. before "
        "count_purchase_orders(vendor=...) or get_vendor_details). Client-side "
        "substring match over a capped sample of suppliers — if it returns no "
        "matches, say so rather than assuming the vendor doesn't exist; a very "
        "large tenant could have more suppliers than the sample covers.",
        {
            "name": {"type": "string", "description": "Vendor name or partial name, as the user wrote it."},
        },
        ["name"],
    ),
]

# --- policy knowledge base (build step 4) ---------------------------

POLICY_TOOL_NAME = "search_policy_docs"

POLICY_TOOL_SPEC: dict = _fn(
    POLICY_TOOL_NAME,
    "Search the approved AP / procurement / finance POLICY knowledge base for "
    "guidance. Use this for any question about policy, process, rules, "
    "thresholds, or 'how do I…' — e.g. travel & expense rules, purchase-order "
    "and requisition approval limits, vendor onboarding steps, invoice payment "
    "terms and three-way-match tolerances. Do NOT use it for live transaction "
    "status (use the S/4HANA lookup tools for a specific invoice / payment / "
    "PO / PR / vendor). Returns grounded=false when nothing relevant is found — "
    "when that happens, do not answer from general knowledge; hand off.",
    {
        "question": {
            "type": "string",
            "description": "The user's policy / process question, in their own words.",
        }
    },
    ["question"],
)

TOOL_SPECS.append(POLICY_TOOL_SPEC)

# S/4HANA schemas and handlers stay in lock-step; the policy tool is dispatched
# separately (against the vector index, not the S4 client).
TOOL_NAMES = frozenset(_HANDLERS)
assert {s["function"]["name"] for s in TOOL_SPECS} == TOOL_NAMES | {POLICY_TOOL_NAME}


def dispatch_tool(name: str, raw_arguments: str | None, client: S4HANAClient) -> ToolOutcome:
    """Execute one tool call. Never raises — failures are returned as content."""
    handler = _HANDLERS.get(name)
    if handler is None:
        logger.warning("model called unknown tool %r", name)
        return ToolOutcome({"error": f"unknown tool {name!r}"}, grounded=False)

    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return ToolOutcome({"error": "tool arguments were not valid JSON"}, grounded=False)
    if not isinstance(args, dict):
        return ToolOutcome({"error": "tool arguments must be a JSON object"}, grounded=False)

    try:
        outcome = handler(client, args)
    except _MissingArg as exc:
        return ToolOutcome(
            {"error": f"missing required argument {exc.key!r} — ask the user for it"}, grounded=False
        )
    except S4HANAError as exc:
        logger.warning("tool %s failed: %s", name, exc)
        return ToolOutcome({"error": f"S/4HANA lookup failed: {exc}"}, grounded=False)
    except ValueError as exc:
        # A malformed argument the handler couldn't coerce (e.g. an unparsable
        # date_from/date_to, or an unknown `period` value) — a model mistake,
        # not an S/4HANA failure. Without this, it would propagate out of
        # dispatch_tool despite this function's contract of never raising.
        logger.warning("tool %s got an invalid argument: %s", name, exc)
        return ToolOutcome({"error": f"invalid tool argument: {exc}"}, grounded=False)

    return outcome


_GROUNDED_MSG = (
    "Answer using ONLY these passages. Name the source ONCE per distinct "
    "section, not after every sentence, as a short trailing clause like 'per "
    "the Travel Policy, section 4.2'. If several facts in a row come from the "
    "same section, cite it once and let the rest of that paragraph ride on "
    "it. Do not wrap the citation in parentheses or set it off with a dash. "
    "If they don't fully cover the question, say which part isn't covered "
    "and point the user to the AHF finance support team."
)
_NOT_GROUNDED_MSG = (
    "No sufficiently relevant policy passage was found. Do NOT answer from "
    "general knowledge. Tell the user this policy isn't in the knowledge base "
    "yet and direct them to the AHF finance support team."
)


def dispatch_policy_tool(raw_arguments: str | None, index=None) -> ToolOutcome:
    """Execute one ``search_policy_docs`` call. Never raises.

    ``index`` defaults to the process-wide :func:`knowledge_base.get_index`
    singleton; tests pass a fake. A knowledge-base failure returns a
    ``grounded: false`` outcome so the model hands off rather than the turn
    crashing.
    """
    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return ToolOutcome({"grounded": False, "results": [], "message": "tool arguments were not valid JSON"}, grounded=False)
    if not isinstance(args, dict):
        return ToolOutcome({"grounded": False, "results": [], "message": "tool arguments must be a JSON object"}, grounded=False)

    question = str(args.get("question") or "").strip()
    if not question:
        return ToolOutcome(
            {"grounded": False, "results": [], "message": "missing required argument 'question' — ask the user for it"},
            grounded=False,
        )

    if index is None:
        from ahf_finance_agent.knowledge_base import get_index

        index = get_index()

    snippet_chars = get_settings().kb_snippet_chars
    try:
        hits, grounded = index.retrieve(question)
    except Exception as exc:  # never crash the turn on a KB problem
        logger.exception("search_policy_docs failed")
        return ToolOutcome(
            {
                "grounded": False,
                "results": [],
                "message": f"Knowledge base unavailable ({exc}). Escalate to the AHF finance support team.",
            },
            grounded=False,
        )

    results = [
        {
            "snippet": h.text[:snippet_chars],
            "title": h.title,
            "section": h.section,
            "source": h.source,
            "score": round(h.score, 3),
        }
        for h in hits
    ]
    logger.info(
        "tool call: name=%s grounded=%s hits=%d top_score=%s",
        POLICY_TOOL_NAME, grounded, len(results), results[0]["score"] if results else None,
    )
    return ToolOutcome(
        {
            "grounded": grounded,
            "results": results,
            "message": _GROUNDED_MSG if grounded else _NOT_GROUNDED_MSG,
        },
        grounded=grounded,
    )


def render_tool_content(outcome: ToolOutcome) -> str:
    """Serialise a tool outcome for the ``role: tool`` message, size-capped."""
    text = json.dumps(outcome.content, default=str, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '… (truncated)"}'
    return text
