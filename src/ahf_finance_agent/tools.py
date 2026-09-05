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


def _int_arg(a: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = a.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


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


_HANDLERS: dict[str, Callable[[S4HANAClient, dict], ToolOutcome]] = {
    "get_invoice_status": _get_invoice_status,
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
        "blocked / posted'.",
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

    return outcome


_GROUNDED_MSG = (
    "Answer using ONLY these passages. Cite each fact inline as "
    "'(<title> — <section>)'. If they don't fully cover the question, say which "
    "part isn't covered and point the user to the AHF finance support team."
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
