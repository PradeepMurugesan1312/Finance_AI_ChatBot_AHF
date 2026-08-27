"""Tool definitions and dispatch for the S/4HANA lookups (build step 3).

:data:`TOOL_SPECS` is the OpenAI-style ``tools`` array handed to GPT 5.2 on
every answering turn. :func:`dispatch_tool` runs one tool call against an
:class:`~ahf_finance_agent.s4hana.S4HANAClient` and returns a
:class:`ToolOutcome` — a JSON-serialisable payload plus a ``grounded`` flag the
answering loop uses to mark the turn as data-backed.

Failures never raise out of here: a missing argument, bad JSON, an unknown
tool, or an S/4HANA error all come back as ``{"error": …}`` so the model can
ask a clarifying question or hand off, rather than the turn crashing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

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
    return _record(c.get_invoice_status(_req(a, "invoice"), _req(a, "fiscal_year")))


def _search_invoices_by_vendor(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _records(c.search_invoices_by_vendor(_req(a, "vendor"), a.get("company_code") or None))


def _get_payment_clearing_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(
        c.get_payment_clearing_status(
            _req(a, "accounting_document"), _req(a, "fiscal_year"), _req(a, "company_code")
        )
    )


def _get_purchase_order_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_order_status(_req(a, "purchase_order")))


def _get_purchase_requisition_status(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_purchase_requisition_status(_req(a, "purchase_requisition")))


def _get_vendor_details(c: S4HANAClient, a: dict) -> ToolOutcome:
    return _record(c.get_vendor_details(_req(a, "business_partner")))


_HANDLERS: dict[str, Callable[[S4HANAClient, dict], ToolOutcome]] = {
    "get_invoice_status": _get_invoice_status,
    "search_invoices_by_vendor": _search_invoices_by_vendor,
    "get_payment_clearing_status": _get_payment_clearing_status,
    "get_purchase_order_status": _get_purchase_order_status,
    "get_purchase_requisition_status": _get_purchase_requisition_status,
    "get_vendor_details": _get_vendor_details,
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
        "Look up the status and header of one specific SAP S/4HANA supplier invoice "
        "by invoice number and fiscal year (payment terms, blocking reason, linked "
        "accounting document). Use for 'is invoice X paid / blocked / posted'.",
        {
            "invoice": {"type": "string", "description": "Supplier invoice number, e.g. 5105601234"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year, e.g. 2026"},
        },
        ["invoice", "fiscal_year"],
    ),
    _fn(
        "search_invoices_by_vendor",
        "List recent supplier invoices and their status for a given vendor "
        "(invoicing party) in SAP S/4HANA, most recent first.",
        {
            "vendor": {"type": "string", "description": "Vendor / invoicing party number, e.g. 100000"},
            "company_code": {"type": "string", "description": "Optional company code to narrow the search, e.g. 1000"},
        },
        ["vendor"],
    ),
    _fn(
        "get_payment_clearing_status",
        "Check whether a payment has actually posted (cleared) against an accounting "
        "document in SAP S/4HANA, as opposed to the invoice merely being posted or "
        "blocked. Returns isCleared plus the clearing date.",
        {
            "accounting_document": {"type": "string", "description": "Accounting document number, e.g. 1400000123"},
            "fiscal_year": {"type": "string", "description": "4-digit fiscal year, e.g. 2026"},
            "company_code": {"type": "string", "description": "Company code, e.g. 1000"},
        },
        ["accounting_document", "fiscal_year", "company_code"],
    ),
    _fn(
        "get_purchase_order_status",
        "Look up a purchase order's header, release/approval state, and processing "
        "status in SAP S/4HANA by PO number.",
        {"purchase_order": {"type": "string", "description": "Purchase order number, e.g. 4500001234"}},
        ["purchase_order"],
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
]

TOOL_NAMES = frozenset(_HANDLERS)
assert {s["function"]["name"] for s in TOOL_SPECS} == TOOL_NAMES  # schemas and handlers in lock-step


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


def render_tool_content(outcome: ToolOutcome) -> str:
    """Serialise a tool outcome for the ``role: tool`` message, size-capped."""
    text = json.dumps(outcome.content, default=str, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '… (truncated)"}'
    return text
