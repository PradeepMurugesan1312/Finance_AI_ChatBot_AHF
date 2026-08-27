from __future__ import annotations

import httpx
import pytest

from ahf_finance_agent import s4hana as s4
from ahf_finance_agent.btp.destinations import DestinationError, ResolvedDestination
from ahf_finance_agent.config import Settings
from ahf_finance_agent.s4hana import S4HANAClient, S4HANAError


def _settings(**over) -> Settings:
    base = dict(app_env="test", _env_file=None)
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeClient:
    """Stand-in for httpx.Client — routes GET by URL substring to canned bodies."""

    routes: dict[str, dict] = {}
    requests: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        full = self.kwargs.get("base_url", "") + url
        _FakeClient.requests.append({"method": "GET", "url": url, "params": params or {}, "client_kwargs": self.kwargs})
        for needle, spec in _FakeClient.routes.items():
            if needle in url:
                req = httpx.Request("GET", full)
                reject = spec.get("reject_field")
                if reject and reject in (params or {}).get("$select", ""):
                    return httpx.Response(
                        404,
                        json={"error": {"message": {"value": f"Resource not found for the segment '{reject}'."}}},
                        request=req,
                    )
                if "text" in spec:
                    return httpx.Response(spec.get("status", 200), text=spec["text"], request=req)
                return httpx.Response(spec.get("status", 200), json=spec["json"], request=req)
        return httpx.Response(404, json={"error": {"message": {"value": "not found"}}}, request=httpx.Request("GET", full))


@pytest.fixture
def fake_s4(monkeypatch):
    _FakeClient.routes = {}
    _FakeClient.requests = []

    def fake_resolve(name, **kw):
        # Bare host — the client prepends the OData service root itself.
        return ResolvedDestination(
            name=name,
            url="https://s4.example.com",
            headers={"Authorization": "Bearer s4-token", "Accept": "application/json"},
            proxy=None,
            authentication="OAuth2ClientCredentials",
            proxy_type="Internet",
        )

    monkeypatch.setattr(s4, "resolve_destination", fake_resolve)
    monkeypatch.setattr(s4.httpx, "Client", _FakeClient)
    return _FakeClient


def _d(results):
    return {"d": {"results": results}} if isinstance(results, list) else {"d": results}


# --- transport --------------------------------------------------------

def test_only_ever_issues_get(fake_s4):
    # The fake only implements .get(); a .post()/.patch() attempt would AttributeError.
    assert not hasattr(fake_s4(), "post")
    assert not hasattr(fake_s4(), "patch")
    assert not hasattr(fake_s4(), "delete")


def test_passes_destination_auth_and_timeout(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {"json": _d({"PurchaseOrder": "4500001234"})}
    S4HANAClient(_settings(s4hana_timeout_seconds=7.5)).get_purchase_order_status("4500001234")
    ck = fake_s4.requests[-1]["client_kwargs"]
    assert ck["base_url"] == "https://s4.example.com"
    assert ck["headers"]["Authorization"] == "Bearer s4-token"
    assert ck["timeout"] == 7.5
    assert fake_s4.requests[-1]["params"]["$format"] == "json"


def test_prepends_odata_service_root_by_default(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {"json": _d({"PurchaseOrder": "4500001234"})}
    S4HANAClient(_settings()).get_purchase_order_status("4500001234")
    assert fake_s4.requests[-1]["url"] == (
        "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('4500001234')"
    )


def test_odata_service_root_is_configurable(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {"json": _d({"PurchaseOrder": "4500001234"})}
    S4HANAClient(_settings(s4hana_odata_base_path="/sap/opu/odata/SAP")).get_purchase_order_status("4500001234")
    assert fake_s4.requests[-1]["url"].startswith("/sap/opu/odata/SAP/API_PURCHASEORDER_PROCESS_SRV/")


def test_empty_odata_service_root_leaves_path_untouched(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {"json": _d({"PurchaseOrder": "4500001234"})}
    S4HANAClient(_settings(s4hana_odata_base_path="")).get_purchase_order_status("4500001234")
    assert fake_s4.requests[-1]["url"] == "/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('4500001234')"


def test_bare_gateway_404_raises_instead_of_looking_like_no_record(fake_s4):
    # ICF 404 (wrong service root / inactive service): HTML body, no OData error.
    fake_s4.routes["/A_PurchaseOrder("] = {"status": 404, "text": "<html><body>404 Not Found</body></html>"}
    with pytest.raises(S4HANAError, match="S4HANA_ODATA_BASE_PATH"):
        S4HANAClient(_settings()).get_purchase_order_status("4500001234")


def test_destination_failure_becomes_s4hana_error(fake_s4, monkeypatch):
    def boom(name, **kw):
        raise DestinationError("no binding")

    monkeypatch.setattr(s4, "resolve_destination", boom)
    with pytest.raises(S4HANAError, match="Cannot resolve S/4HANA destination 'S43'"):
        S4HANAClient(_settings()).get_purchase_order_status("4500001234")


def test_404_is_no_record_not_an_error(fake_s4):
    # no route registered -> fake returns 404
    assert S4HANAClient(_settings()).get_purchase_order_status("4500000000") is None


def test_5xx_raises_with_odata_message(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {"status": 500, "json": {"error": {"message": {"value": "backend down"}}}}
    with pytest.raises(S4HANAError, match="backend down"):
        S4HANAClient(_settings()).get_purchase_order_status("4500001234")


# --- filter safety --------------------------------------------------

def test_rejects_suspicious_identifier_before_calling_s4(fake_s4):
    with pytest.raises(S4HANAError, match="suspicious identifier"):
        S4HANAClient(_settings()).get_purchase_order_status("4500001234' or '1'eq'1")
    assert fake_s4.requests == []  # never left the process


# --- projections & sensitive-key stripping -------------------------

def test_invoice_status_selects_no_sensitive_field_and_strips_any_that_appear(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d(
            [
                {
                    "__metadata": {"uri": "x"},
                    "SupplierInvoice": "5105601234",
                    "FiscalYear": "2026",
                    "SupplierInvoiceStatus": "5",
                    "PaymentBlockingReason": "",
                    "IBAN": "DE89370400440532013000",  # must never survive
                    "to_SupplierInvoiceItem": {"__deferred": {"uri": "y"}},
                }
            ]
        )
    }
    rec = S4HANAClient(_settings()).get_invoice_status("5105601234", "2026")
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "SupplierInvoice eq '5105601234' and FiscalYear eq '2026'"
    select = params["$select"].split(",")
    assert "IBAN" not in select and "BankAccount" not in select
    assert not any(f.lower().startswith(("bank", "iban", "swift", "taxnumber")) for f in select)
    assert "IBAN" not in rec
    assert "__metadata" not in rec and "to_SupplierInvoiceItem" not in rec
    assert rec["SupplierInvoice"] == "5105601234"


def test_invoice_status_none_when_filter_returns_nothing(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_invoice_status("5105601234", "2026") is None


def test_invoice_status_by_number_only_does_not_filter_on_fiscal_year(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{"SupplierInvoice": "5100000017", "FiscalYear": "2017"}])
    }
    rec = S4HANAClient(_settings()).get_invoice_status("5100000017")
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "SupplierInvoice eq '5100000017'"
    assert params["$orderby"] == "PostingDate desc"
    assert rec["FiscalYear"] == "2017"


def test_select_drops_release_specific_field_and_retries(fake_s4):
    # Gateway 404s the whole request when $select names an unknown field.
    fake_s4.routes["/A_PurchaseOrder("] = {
        "reject_field": "PurchasingProcessingStatus",
        "json": _d({"PurchaseOrder": "4500001234"}),
    }
    rec = S4HANAClient(_settings()).get_purchase_order_status("4500001234")
    assert rec["PurchaseOrder"] == "4500001234"
    assert len(fake_s4.requests) == 2  # first rejected, retried without the field
    assert "PurchasingProcessingStatus" not in fake_s4.requests[-1]["params"].get("$select", "")


def test_unknown_entity_segment_is_a_real_error(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {
        "status": 404,
        "json": {"error": {"message": {"value": "Resource not found for the segment 'A_PurchaseOrder'."}}},
    }
    with pytest.raises(S4HANAError, match="no resource/segment"):
        S4HANAClient(_settings()).get_purchase_order_status("4500001234")


def test_search_invoices_by_vendor_builds_filter_and_orders_recent_first(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {"json": _d([{"SupplierInvoice": "1"}, {"SupplierInvoice": "2"}])}
    rows = S4HANAClient(_settings()).search_invoices_by_vendor("100000", "1000", top=5)
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "InvoicingParty eq '100000' and CompanyCode eq '1000'"
    assert params["$orderby"] == "PostingDate desc"
    assert params["$top"] == "5"
    assert len(rows) == 2


def test_payment_clearing_status_summarises_cleared_flag(fake_s4):
    fake_s4.routes["/A_JournalEntryItemBasic"] = {
        "json": _d(
            [
                {"AccountingDocument": "1400000123", "ClearingDate": "/Date(0)/"},
                {"AccountingDocument": "1400000123", "ClearingDate": "/Date(1719360000000)/"},
            ]
        )
    }
    out = S4HANAClient(_settings()).get_payment_clearing_status("1400000123", "2026", "1000")
    assert out["isCleared"] is True
    assert out["clearingDate"] == "/Date(1719360000000)/"
    assert len(out["items"]) == 2


def test_payment_clearing_status_none_when_no_items(fake_s4):
    fake_s4.routes["/A_JournalEntryItemBasic"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_payment_clearing_status("1400000123", "2026", "1000") is None


def test_purchase_requisition_merges_header_and_items(fake_s4):
    fake_s4.routes["/A_PurchaseRequisitionHeader("] = {"json": _d({"PurchaseRequisition": "1000005678"})}
    fake_s4.routes["/A_PurchaseRequisitionItem"] = {
        "json": _d([{"PurchaseRequisitionItem": "10", "ProcessingStatus": "N"}])
    }
    out = S4HANAClient(_settings()).get_purchase_requisition_status("1000005678")
    assert out["PurchaseRequisition"] == "1000005678"
    assert out["items"][0]["ProcessingStatus"] == "N"


def test_vendor_details_attaches_supplier_block_status_and_never_returns_bank(fake_s4):
    fake_s4.routes["/A_BusinessPartner("] = {
        "json": _d({"BusinessPartner": "100000", "BusinessPartnerIsBlocked": False, "BankAccount": "12345678"})
    }
    fake_s4.routes["/A_Supplier("] = {
        "json": _d({"Supplier": "100000", "PostingIsBlocked": False, "TaxNumber1": "AB123"})
    }
    out = S4HANAClient(_settings()).get_vendor_details("100000")
    assert out["BusinessPartner"] == "100000"
    assert "BankAccount" not in out
    assert out["supplierCompanyData"]["PostingIsBlocked"] is False
    assert "TaxNumber1" not in out["supplierCompanyData"]


def test_vendor_details_survives_supplier_view_error(fake_s4):
    fake_s4.routes["/A_BusinessPartner("] = {"json": _d({"BusinessPartner": "100000"})}
    fake_s4.routes["/A_Supplier("] = {"status": 500, "json": {"error": {"message": {"value": "no auth"}}}}
    out = S4HANAClient(_settings()).get_vendor_details("100000")
    assert out["BusinessPartner"] == "100000"
    assert "supplierCompanyData" not in out


def test_ping_hits_business_partner_with_top_1(fake_s4):
    fake_s4.routes["/A_BusinessPartner"] = {"json": _d([{"BusinessPartner": "1"}])}
    out = S4HANAClient(_settings()).ping()
    assert out == {"reachable": True, "rows": 1}
    assert fake_s4.requests[-1]["params"]["$top"] == "1"
    assert fake_s4.requests[-1]["params"]["$select"] == "BusinessPartner"
