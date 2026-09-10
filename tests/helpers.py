"""Shared test doubles."""

from __future__ import annotations

from ahf_finance_agent.knowledge_base import Retrieved
from ahf_finance_agent.llm import ChatResult, ToolCall


def tool_call_result(name: str, arguments: str = "{}", *, call_id: str = "call-1") -> ChatResult:
    """A ChatResult representing the model asking to invoke one tool."""
    call = ToolCall(id=call_id, name=name, raw_arguments=arguments)
    return ChatResult(
        text="",
        finish_reason="tool_calls",
        model="gpt-5.2-test",
        prompt_tokens=10,
        completion_tokens=0,
        latency_ms=3,
        tool_calls=[call],
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            ],
        },
    )


def text_result(text: str) -> ChatResult:
    return ChatResult(
        text=text,
        finish_reason="stop",
        model="gpt-5.2-test",
        prompt_tokens=11,
        completion_tokens=7,
        latency_ms=5,
    )


class FakeGenAIHubClient:
    """Stand-in for GenAIHubClient — records calls, returns canned results.

    Pass ``script`` (a list of ChatResult) to drive a multi-turn tool loop;
    each ``chat`` call pops the next one. Falls back to ``reply`` text once the
    script is exhausted (or if no script was given).
    """

    def __init__(
        self,
        reply: str = "Fake model reply.",
        *,
        raise_exc: Exception | None = None,
        script: list[ChatResult] | None = None,
    ):
        self.reply = reply
        self.raise_exc = raise_exc
        self.script = list(script) if script else None
        self.calls: list[list[dict]] = []
        self.tool_specs_seen: list[list[dict] | None] = []

    def chat(self, messages, *, tools=None, tool_choice=None, max_completion_tokens=None):
        self.calls.append(list(messages))
        self.tool_specs_seen.append(tools)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.script:
            return self.script.pop(0)
        return text_result(self.reply)


def retrieved(title: str, section: str, text: str, *, source: str = "sample.md", score: float = 0.7) -> Retrieved:
    return Retrieved(text=text, source=source, title=title, section=section, score=score)


class FakeVectorIndex:
    """Stand-in for knowledge_base.VectorIndex — canned (hits, grounded)."""

    def __init__(self, hits: list[Retrieved] | None = None, grounded: bool = False, *, raise_exc: Exception | None = None):
        self._hits = hits or []
        self._grounded = grounded
        self._raise_exc = raise_exc
        self.queries: list[str] = []

    def retrieve(self, query: str, k=None, min_score=None):
        self.queries.append(query)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._hits, self._grounded

    def chunk_count(self) -> int:
        return len(self._hits)


class FakeS4HANAClient:
    """Stand-in for S4HANAClient — canned return per method name."""

    def __init__(self, **returns):
        # e.g. FakeS4HANAClient(get_invoice_status={"SupplierInvoice": "5105601234"})
        self._returns = returns
        self.calls: list[tuple[str, tuple, dict]] = []

    def _canned(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        value = self._returns.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def get_invoice_status(self, *a, **k):
        return self._canned("get_invoice_status", *a, **k)

    def search_invoices_by_vendor(self, *a, **k):
        return self._canned("search_invoices_by_vendor", *a, **k) or []

    def get_payment_clearing_status(self, *a, **k):
        return self._canned("get_payment_clearing_status", *a, **k)

    def get_invoice_payment_status(self, *a, **k):
        return self._canned("get_invoice_payment_status", *a, **k)

    def get_purchase_order_status(self, *a, **k):
        return self._canned("get_purchase_order_status", *a, **k)

    def get_purchase_order_approval_status(self, *a, **k):
        return self._canned("get_purchase_order_approval_status", *a, **k)

    def get_goods_receipts_for_po(self, *a, **k):
        return self._canned("get_goods_receipts_for_po", *a, **k)

    def get_invoice_items(self, *a, **k):
        return self._canned("get_invoice_items", *a, **k) or []

    def get_purchase_order_items(self, *a, **k):
        return self._canned("get_purchase_order_items", *a, **k) or []

    def get_purchase_order_delivery_schedule(self, *a, **k):
        return self._canned("get_purchase_order_delivery_schedule", *a, **k)

    def check_three_way_match(self, *a, **k):
        return self._canned("check_three_way_match", *a, **k)

    def get_purchase_requisition_status(self, *a, **k):
        return self._canned("get_purchase_requisition_status", *a, **k)

    def get_vendor_details(self, *a, **k):
        return self._canned("get_vendor_details", *a, **k)

    def get_vendor_email_addresses(self, *a, **k):
        return self._canned("get_vendor_email_addresses", *a, **k)

    def get_vendor_bank_accounts(self, *a, **k):
        return self._canned("get_vendor_bank_accounts", *a, **k)

    def get_budget_status(self, *a, **k):
        return self._canned("get_budget_status", *a, **k) or {"budgetAvailable": False}

    def get_company_code_details(self, *a, **k):
        return self._canned("get_company_code_details", *a, **k)

    def get_cost_center_details(self, *a, **k):
        return self._canned("get_cost_center_details", *a, **k)

    def get_profit_center_details(self, *a, **k):
        return self._canned("get_profit_center_details", *a, **k)

    def get_gl_account_master(self, *a, **k):
        return self._canned("get_gl_account_master", *a, **k)

    def get_gl_account_activity(self, *a, **k):
        return self._canned("get_gl_account_activity", *a, **k)

    def get_accounts_payable_summary(self, *a, **k):
        return self._canned("get_accounts_payable_summary", *a, **k) or {"connected": False}

    def get_accounts_receivable_summary(self, *a, **k):
        return self._canned("get_accounts_receivable_summary", *a, **k) or {"connected": False}

    def list_open_invoices_for_vendor(self, *a, **k):
        return self._canned("list_open_invoices_for_vendor", *a, **k) or {"connected": False, "invoices": []}

    def get_largest_open_item(self, *a, **k):
        return self._canned("get_largest_open_item", *a, **k) or {"connected": False, "largest": None}

    def get_ap_aging_summary(self, *a, **k):
        return self._canned("get_ap_aging_summary", *a, **k) or {"connected": False, "buckets": None}

    def get_top_vendors_by_open_payable(self, *a, **k):
        return self._canned("get_top_vendors_by_open_payable", *a, **k) or {"connected": False, "vendors": []}

    def get_average_days_to_clear(self, *a, **k):
        return self._canned("get_average_days_to_clear", *a, **k) or {"connected": False, "averageDays": None}

    def list_open_invoices_for_customer(self, *a, **k):
        return self._canned("list_open_invoices_for_customer", *a, **k) or {"connected": False, "invoices": []}

    def get_largest_open_receivable(self, *a, **k):
        return self._canned("get_largest_open_receivable", *a, **k) or {"connected": False, "largest": None}

    def get_ar_aging_summary(self, *a, **k):
        return self._canned("get_ar_aging_summary", *a, **k) or {"connected": False, "buckets": None}

    def get_top_customers_by_open_receivable(self, *a, **k):
        return self._canned("get_top_customers_by_open_receivable", *a, **k) or {"connected": False, "customers": []}

    def get_average_days_to_collect(self, *a, **k):
        return self._canned("get_average_days_to_collect", *a, **k) or {"connected": False, "averageDays": None}

    def count_purchase_orders(self, *a, **k):
        return self._canned("count_purchase_orders", *a, **k) or {"connected": False}

    def count_purchase_requisitions(self, *a, **k):
        return self._canned("count_purchase_requisitions", *a, **k) or {"connected": False}

    def count_supplier_invoices(self, *a, **k):
        return self._canned("count_supplier_invoices", *a, **k) or {"connected": False}

    def count_invoices_by_fiscal_period(self, *a, **k):
        return self._canned("count_invoices_by_fiscal_period", *a, **k) or {"connected": False}

    def count_goods_receipts(self, *a, **k):
        return self._canned("count_goods_receipts", *a, **k) or {"connected": False}

    def count_pos_overdue_without_goods_receipt(self, *a, **k):
        return self._canned("count_pos_overdue_without_goods_receipt", *a, **k) or {"connected": False}

    def count_cleared_documents(self, *a, **k):
        return self._canned("count_cleared_documents", *a, **k) or {"connected": False}

    def count_new_vendors(self, *a, **k):
        return self._canned("count_new_vendors", *a, **k) or {"connected": False}

    def count_blocked_vendors(self, *a, **k):
        return self._canned("count_blocked_vendors", *a, **k) or {"connected": False}

    def search_vendors_by_name(self, *a, **k):
        return self._canned("search_vendors_by_name", *a, **k) or {"connected": False, "matches": []}
