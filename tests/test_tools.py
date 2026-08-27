from __future__ import annotations

import json

from ahf_finance_agent.s4hana import S4HANAError
from ahf_finance_agent.tools import TOOL_NAMES, TOOL_SPECS, dispatch_tool, render_tool_content
from tests.helpers import FakeS4HANAClient


def test_specs_and_handlers_are_in_lockstep():
    spec_names = {s["function"]["name"] for s in TOOL_SPECS}
    assert spec_names == set(TOOL_NAMES)
    assert spec_names == {
        "get_invoice_status",
        "search_invoices_by_vendor",
        "get_payment_clearing_status",
        "get_purchase_order_status",
        "get_purchase_requisition_status",
        "get_vendor_details",
    }


def test_every_spec_is_a_valid_openai_function_tool():
    for spec in TOOL_SPECS:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        for req in params["required"]:
            assert req in params["properties"]


def test_dispatch_routes_to_client_and_marks_grounded_on_hit():
    s4 = FakeS4HANAClient(get_invoice_status={"SupplierInvoice": "5105601234"})
    outcome = dispatch_tool("get_invoice_status", '{"invoice": "5105601234", "fiscal_year": "2026"}', s4)
    assert s4.calls == [("get_invoice_status", ("5105601234", "2026"), {})]
    assert outcome.grounded is True
    assert outcome.content == {"found": True, "record": {"SupplierInvoice": "5105601234"}}


def test_dispatch_miss_is_not_grounded():
    s4 = FakeS4HANAClient(get_purchase_order_status=None)
    outcome = dispatch_tool("get_purchase_order_status", '{"purchase_order": "4500009999"}', s4)
    assert outcome.grounded is False
    assert outcome.content["found"] is False


def test_search_returns_count_and_records():
    s4 = FakeS4HANAClient(search_invoices_by_vendor=[{"SupplierInvoice": "1"}, {"SupplierInvoice": "2"}])
    outcome = dispatch_tool("search_invoices_by_vendor", '{"vendor": "100000"}', s4)
    assert outcome.grounded is True
    assert outcome.content == {"count": 2, "records": [{"SupplierInvoice": "1"}, {"SupplierInvoice": "2"}]}


def test_missing_required_argument_is_reported_not_raised():
    s4 = FakeS4HANAClient()
    outcome = dispatch_tool("get_invoice_status", '{"invoice": "5105601234"}', s4)  # no fiscal_year
    assert outcome.grounded is False
    assert "fiscal_year" in outcome.content["error"]
    assert s4.calls == []  # never reached the client


def test_blank_required_argument_counts_as_missing():
    s4 = FakeS4HANAClient()
    outcome = dispatch_tool("get_purchase_order_status", '{"purchase_order": "   "}', s4)
    assert "purchase_order" in outcome.content["error"]


def test_bad_json_arguments_are_reported():
    outcome = dispatch_tool("get_invoice_status", "{not json", FakeS4HANAClient())
    assert "valid JSON" in outcome.content["error"]


def test_unknown_tool_is_reported():
    outcome = dispatch_tool("drop_table", "{}", FakeS4HANAClient())
    assert "unknown tool" in outcome.content["error"]
    assert outcome.grounded is False


def test_s4hana_error_becomes_error_content():
    s4 = FakeS4HANAClient(get_vendor_details=S4HANAError("S/4HANA returned 503"))
    outcome = dispatch_tool("get_vendor_details", '{"business_partner": "100000"}', s4)
    assert outcome.grounded is False
    assert "503" in outcome.content["error"]


def test_render_tool_content_is_json_and_size_capped():
    from ahf_finance_agent.tools import MAX_TOOL_RESULT_CHARS, ToolOutcome

    small = render_tool_content(ToolOutcome({"a": 1}, grounded=True))
    assert json.loads(small) == {"a": 1}

    big = render_tool_content(ToolOutcome({"blob": "x" * (MAX_TOOL_RESULT_CHARS * 2)}, grounded=True))
    assert len(big) <= MAX_TOOL_RESULT_CHARS + 32
    assert big.endswith("truncated)\"}")
