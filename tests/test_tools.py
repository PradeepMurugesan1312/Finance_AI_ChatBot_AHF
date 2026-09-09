from __future__ import annotations

import json

from ahf_finance_agent.s4hana import S4HANAError
from ahf_finance_agent.tools import TOOL_NAMES, TOOL_SPECS, dispatch_tool, render_tool_content
from tests.helpers import FakeS4HANAClient


def test_specs_and_handlers_are_in_lockstep():
    spec_names = {s["function"]["name"] for s in TOOL_SPECS}
    # S/4HANA specs and handlers stay in lock-step; the policy tool rides
    # alongside them in TOOL_SPECS but is dispatched separately.
    assert set(TOOL_NAMES) == {
        "get_invoice_status",
        "get_invoice_items",
        "search_invoices_by_vendor",
        "get_payment_clearing_status",
        "get_invoice_payment_status",
        "get_purchase_order_status",
        "get_purchase_order_items",
        "get_purchase_order_delivery_schedule",
        "get_purchase_order_approval_status",
        "check_three_way_match",
        "get_goods_receipts_for_po",
        "get_purchase_requisition_status",
        "get_vendor_details",
        "get_vendor_email_addresses",
        "get_vendor_bank_accounts",
        "get_budget_status",
        "get_company_code_details",
        "get_cost_center_details",
        "get_profit_center_details",
        "get_gl_account_master",
        "get_gl_account_activity",
        "get_accounts_payable_summary",
        "get_accounts_receivable_summary",
    }
    assert spec_names == set(TOOL_NAMES) | {"search_policy_docs"}


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
    assert outcome.content["count"] == 2
    assert outcome.content["records"] == [{"SupplierInvoice": "1"}, {"SupplierInvoice": "2"}]
    # paging metadata so the model can answer "give me 10 more"
    assert outcome.content["skip"] == 0
    assert outcome.content["next_skip"] == 2


def test_search_paging_passes_skip_and_limit_through():
    s4 = FakeS4HANAClient(search_invoices_by_vendor=[{"SupplierInvoice": "11"}])
    outcome = dispatch_tool("search_invoices_by_vendor", '{"vendor": "V", "limit": 25, "skip": 10}', s4)
    _, _, kwargs = s4.calls[0]
    assert kwargs == {"top": 25, "skip": 10}
    assert outcome.content["skip"] == 10
    assert outcome.content["next_skip"] == 11


def test_dispatch_goods_receipts_for_po_marks_grounded_on_hit():
    s4 = FakeS4HANAClient(
        get_goods_receipts_for_po={
            "purchaseOrder": "4500001234",
            "goodsReceiptItemCount": 1,
            "hasActiveGoodsReceipt": True,
            "items": [{"MaterialDocument": "5000000001", "GoodsMovementType": "101"}],
        }
    )
    outcome = dispatch_tool("get_goods_receipts_for_po", '{"purchase_order": "4500001234"}', s4)
    assert s4.calls == [("get_goods_receipts_for_po", ("4500001234",), {})]
    assert outcome.grounded is True
    assert outcome.content["record"]["hasActiveGoodsReceipt"] is True


def test_dispatch_goods_receipts_miss_is_not_grounded():
    s4 = FakeS4HANAClient(get_goods_receipts_for_po=None)
    outcome = dispatch_tool("get_goods_receipts_for_po", '{"purchase_order": "4500009999"}', s4)
    assert outcome.grounded is False
    assert outcome.content["found"] is False


def test_dispatch_purchase_order_approval_status():
    s4 = FakeS4HANAClient(
        get_purchase_order_approval_status={
            "PurchaseOrder": "4500001234",
            "approvalSummary": {"isReleased": False, "releaseIncomplete": True},
        }
    )
    outcome = dispatch_tool("get_purchase_order_approval_status", '{"purchase_order": "4500001234"}', s4)
    assert s4.calls == [("get_purchase_order_approval_status", ("4500001234",), {})]
    assert outcome.grounded is True
    assert outcome.content["record"]["approvalSummary"]["isReleased"] is False


def test_dispatch_invoice_items_requires_invoice_and_fiscal_year():
    s4 = FakeS4HANAClient(get_invoice_items=[{"SupplierInvoiceItem": "1", "PurchaseOrder": "4500001234"}])
    ok = dispatch_tool("get_invoice_items", '{"invoice": "5105601234", "fiscal_year": "2026"}', s4)
    assert s4.calls == [("get_invoice_items", ("5105601234", "2026"), {})]
    assert ok.grounded is True
    assert ok.content["count"] == 1

    s4b = FakeS4HANAClient()
    missing = dispatch_tool("get_invoice_items", '{"invoice": "5105601234"}', s4b)  # no fiscal_year
    assert missing.grounded is False
    assert "fiscal_year" in missing.content["error"]
    assert s4b.calls == []

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "get_invoice_items")
    assert spec["function"]["parameters"]["required"] == ["invoice", "fiscal_year"]


def test_dispatch_invoice_payment_status_takes_just_the_invoice_number():
    s4 = FakeS4HANAClient(
        get_invoice_payment_status={
            "invoice": "5100000016",
            "accountingDocument": "5100000016",
            "accountingDocumentSource": "assumed_equal_to_invoice_number",
            "paymentCleared": False,
        }
    )
    outcome = dispatch_tool("get_invoice_payment_status", '{"invoice": "5100000016"}', s4)
    assert s4.calls == [("get_invoice_payment_status", ("5100000016", None), {})]
    assert outcome.grounded is True
    assert outcome.content["record"]["accountingDocumentSource"] == "assumed_equal_to_invoice_number"

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "get_invoice_payment_status")
    assert spec["function"]["parameters"]["required"] == ["invoice"]


def test_dispatch_purchase_order_delivery_schedule():
    s4 = FakeS4HANAClient(
        get_purchase_order_delivery_schedule={
            "purchaseOrder": "4500000001",
            "earliestDeliveryDate": "2017-02-01T00:00:00",
            "latestDeliveryDate": "2017-03-15T00:00:00",
            "lines": [{"PurchaseOrderItem": "10", "ScheduleLineDeliveryDate": "2017-02-01T00:00:00"}],
        }
    )
    outcome = dispatch_tool("get_purchase_order_delivery_schedule", '{"purchase_order": "4500000001"}', s4)
    assert s4.calls == [("get_purchase_order_delivery_schedule", ("4500000001",), {})]
    assert outcome.grounded is True
    assert outcome.content["record"]["earliestDeliveryDate"] == "2017-02-01T00:00:00"


def test_dispatch_purchase_order_delivery_schedule_error_is_returned_not_raised():
    s4 = FakeS4HANAClient(
        get_purchase_order_delivery_schedule=S4HANAError("no resource/segment 'A_PurchaseOrderScheduleLine'")
    )
    outcome = dispatch_tool("get_purchase_order_delivery_schedule", '{"purchase_order": "4500000001"}', s4)
    assert outcome.grounded is False
    assert "error" in outcome.content


def test_dispatch_purchase_order_items():
    s4 = FakeS4HANAClient(get_purchase_order_items=[{"PurchaseOrderItem": "10", "OrderQuantity": "100"}])
    outcome = dispatch_tool("get_purchase_order_items", '{"purchase_order": "4500000001"}', s4)
    assert s4.calls == [("get_purchase_order_items", ("4500000001",), {})]
    assert outcome.grounded is True
    assert outcome.content["count"] == 1


def test_dispatch_check_three_way_match_requires_invoice_and_fiscal_year():
    s4 = FakeS4HANAClient(
        check_three_way_match={
            "invoice": "5100000016",
            "isBlockedForPayment": True,
            "paymentBlockingReason": "P",
            "computedMatchSummary": {"priceVarianceDetected": True, "quantityVarianceDetected": True},
        }
    )
    ok = dispatch_tool("check_three_way_match", '{"invoice": "5100000016", "fiscal_year": "2017"}', s4)
    assert s4.calls == [("check_three_way_match", ("5100000016", "2017"), {})]
    assert ok.grounded is True
    assert ok.content["record"]["paymentBlockingReason"] == "P"

    s4b = FakeS4HANAClient()
    missing = dispatch_tool("check_three_way_match", '{"invoice": "5100000016"}', s4b)  # no fiscal_year
    assert missing.grounded is False
    assert "fiscal_year" in missing.content["error"]
    assert s4b.calls == []

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "check_three_way_match")
    assert spec["function"]["parameters"]["required"] == ["invoice", "fiscal_year"]


def test_missing_required_argument_is_reported_not_raised():
    s4 = FakeS4HANAClient()
    outcome = dispatch_tool("get_invoice_status", '{"fiscal_year": "2026"}', s4)  # no invoice
    assert outcome.grounded is False
    assert "invoice" in outcome.content["error"]
    assert s4.calls == []  # never reached the client


def test_invoice_status_does_not_require_fiscal_year():
    s4 = FakeS4HANAClient(get_invoice_status={"SupplierInvoice": "5105601234", "FiscalYear": "2017"})
    outcome = dispatch_tool("get_invoice_status", '{"invoice": "5105601234"}', s4)
    assert outcome.grounded is True
    assert s4.calls == [("get_invoice_status", ("5105601234", None), {})]
    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "get_invoice_status")
    assert spec["function"]["parameters"]["required"] == ["invoice"]


def test_blank_required_argument_counts_as_missing():
    s4 = FakeS4HANAClient()
    outcome = dispatch_tool("get_purchase_order_status", '{"purchase_order": "   "}', s4)
    assert "purchase_order" in outcome.content["error"]


def test_bad_json_arguments_are_reported():
    outcome = dispatch_tool("get_invoice_status", "{not json", FakeS4HANAClient())
    assert "valid JSON" in outcome.content["error"]


def test_dispatch_vendor_email_addresses():
    s4 = FakeS4HANAClient(
        get_vendor_email_addresses=[
            {"addressId": "23421", "contactPerson": None, "emailAddress": "info@17100001.com", "isDefault": True},
        ]
    )
    outcome = dispatch_tool("get_vendor_email_addresses", '{"business_partner": "1000000"}', s4)
    assert s4.calls == [("get_vendor_email_addresses", ("1000000",), {})]
    assert outcome.grounded is True
    assert outcome.content["records"][0]["emailAddress"] == "info@17100001.com"


def test_dispatch_vendor_bank_accounts():
    s4 = FakeS4HANAClient(
        get_vendor_bank_accounts=[
            {"bankIdentification": "0001", "iban": "DE89370400440532013000", "swiftCode": "COBADEFFXXX"},
        ]
    )
    outcome = dispatch_tool("get_vendor_bank_accounts", '{"business_partner": "1000000"}', s4)
    assert s4.calls == [("get_vendor_bank_accounts", ("1000000",), {})]
    assert outcome.grounded is True
    assert outcome.content["records"][0]["iban"] == "DE89370400440532013000"


def test_dispatch_budget_status_ungrounded_when_nothing_computed():
    s4 = FakeS4HANAClient(
        get_budget_status={
            "budgetAvailable": False,
            "costObjectType": "purchase_order",
            "actualSpend": None,
            "commitmentValue": None,
            "handoff": "Check Funds Management 'Budget Consumption' (FMAVCR01), or contact FP&A.",
        }
    )
    outcome = dispatch_tool(
        "get_budget_status",
        '{"cost_object_type": "purchase_order", "cost_object_id": "4500001234"}',
        s4,
    )
    assert s4.calls == [("get_budget_status", ("purchase_order", "4500001234", None), {})]
    assert outcome.grounded is False
    assert outcome.content["budgetAvailable"] is False
    assert "FMAVCR01" in outcome.content["handoff"]

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "get_budget_status")
    assert spec["function"]["parameters"]["properties"]["cost_object_type"]["enum"] == [
        "cost_center", "internal_order", "purchase_order"
    ]


def test_dispatch_budget_status_grounded_when_actual_spend_computed():
    s4 = FakeS4HANAClient(
        get_budget_status={
            "budgetAvailable": False,
            "costObjectType": "cost_center",
            "actualSpend": {"netPostedAmount": 3800.0, "currency": "USD", "postingCount": 2},
            "commitmentValue": None,
            "handoff": "Check the Cost Centers – Plan/Actual app.",
        }
    )
    outcome = dispatch_tool(
        "get_budget_status", '{"cost_object_type": "cost_center", "cost_object_id": "1000"}', s4
    )
    assert outcome.grounded is True
    assert outcome.content["budgetAvailable"] is False  # still true -- only actualSpend is real
    assert outcome.content["actualSpend"]["netPostedAmount"] == 3800.0


def test_dispatch_company_code_details():
    s4 = FakeS4HANAClient(get_company_code_details={"CompanyCode": "1710", "Currency": "USD"})
    outcome = dispatch_tool("get_company_code_details", '{"company_code": "1710"}', s4)
    assert s4.calls == [("get_company_code_details", ("1710",), {})]
    assert outcome.grounded is True
    assert outcome.content["record"]["Currency"] == "USD"


def test_dispatch_cost_center_details_with_optional_controlling_area():
    s4 = FakeS4HANAClient(get_cost_center_details={"CostCenter": "1000", "CostCtrResponsiblePersonName": "BPINST"})
    outcome = dispatch_tool(
        "get_cost_center_details", '{"cost_center": "1000", "controlling_area": "1710"}', s4
    )
    assert s4.calls == [("get_cost_center_details", ("1000", "1710"), {})]
    assert outcome.grounded is True


def test_dispatch_profit_center_details():
    s4 = FakeS4HANAClient(get_profit_center_details=None)
    outcome = dispatch_tool("get_profit_center_details", '{"profit_center": "YB100"}', s4)
    assert s4.calls == [("get_profit_center_details", ("YB100", None), {})]
    assert outcome.grounded is False
    assert outcome.content["found"] is False


def test_dispatch_gl_account_master():
    s4 = FakeS4HANAClient(get_gl_account_master={"GLAccount": "400000", "IsBalanceSheetAccount": False})
    outcome = dispatch_tool("get_gl_account_master", '{"gl_account": "400000"}', s4)
    assert s4.calls == [("get_gl_account_master", ("400000", None), {})]
    assert outcome.grounded is True


def test_dispatch_gl_account_activity_requires_company_code_and_gl_account():
    s4 = FakeS4HANAClient(get_gl_account_activity={"glAccount": "400000", "netPostedAmount": 7500.0})
    ok = dispatch_tool(
        "get_gl_account_activity", '{"company_code": "1710", "gl_account": "400000"}', s4
    )
    assert s4.calls == [("get_gl_account_activity", ("1710", "400000", None), {})]
    assert ok.grounded is True
    assert ok.content["record"]["netPostedAmount"] == 7500.0

    s4b = FakeS4HANAClient()
    missing = dispatch_tool("get_gl_account_activity", '{"gl_account": "400000"}', s4b)  # no company_code
    assert missing.grounded is False
    assert "company_code" in missing.content["error"]
    assert s4b.calls == []


def test_dispatch_accounts_payable_summary():
    s4 = FakeS4HANAClient(
        get_accounts_payable_summary={"connected": True, "openItemCount": 3, "netOpenAmount": 4200.0}
    )
    outcome = dispatch_tool(
        "get_accounts_payable_summary", '{"company_code": "1710", "vendor": "100000"}', s4
    )
    assert s4.calls == [("get_accounts_payable_summary", ("1710", "100000", None), {})]
    assert outcome.grounded is True
    assert outcome.content["openItemCount"] == 3

    s4b = FakeS4HANAClient()
    missing = dispatch_tool("get_accounts_payable_summary", "{}", s4b)  # no company_code
    assert missing.grounded is False
    assert "company_code" in missing.content["error"]
    assert s4b.calls == []


def test_dispatch_accounts_receivable_summary_not_connected_is_not_grounded():
    s4 = FakeS4HANAClient(get_accounts_receivable_summary={"connected": False, "message": "not available"})
    outcome = dispatch_tool("get_accounts_receivable_summary", '{"company_code": "1710"}', s4)
    assert s4.calls == [("get_accounts_receivable_summary", ("1710", None, None), {})]
    assert outcome.grounded is False
    assert outcome.content["connected"] is False


def test_dispatch_accounts_receivable_summary_connected_is_grounded():
    s4 = FakeS4HANAClient(
        get_accounts_receivable_summary={"connected": True, "openItemCount": 1, "netOpenAmount": 900.0}
    )
    outcome = dispatch_tool(
        "get_accounts_receivable_summary", '{"company_code": "1710", "customer": "200000"}', s4
    )
    assert s4.calls == [("get_accounts_receivable_summary", ("1710", "200000", None), {})]
    assert outcome.grounded is True


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
