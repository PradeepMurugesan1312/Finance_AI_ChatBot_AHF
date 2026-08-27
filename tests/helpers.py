"""Shared test doubles."""

from __future__ import annotations

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

    def get_purchase_order_status(self, *a, **k):
        return self._canned("get_purchase_order_status", *a, **k)

    def get_purchase_requisition_status(self, *a, **k):
        return self._canned("get_purchase_requisition_status", *a, **k)

    def get_vendor_details(self, *a, **k):
        return self._canned("get_vendor_details", *a, **k)
