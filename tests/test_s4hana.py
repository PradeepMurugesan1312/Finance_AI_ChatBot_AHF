from __future__ import annotations

import time

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
                probed = (params or {}).get("$select", "") + "|" + (params or {}).get("$filter", "")
                if reject and reject in probed:
                    status = spec.get("reject_status", 404)
                    msg = spec.get("reject_message") or f"Resource not found for the segment '{reject}'."
                    return httpx.Response(
                        status,
                        json={"error": {"message": {"value": msg}}},
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
    reads = [r for r in fake_s4.requests if "A_PurchaseOrder(" in r["url"]]
    assert len(reads) == 2  # first rejected, retried without the field (plus one catalogue probe)
    assert "PurchasingProcessingStatus" not in reads[-1]["params"].get("$select", "")


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
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {
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


def test_purchase_order_approval_status_summarises_release_flags(fake_s4):
    fake_s4.routes["/A_PurchaseOrder("] = {
        "json": _d(
            {
                "PurchaseOrder": "4500001234",
                "PurchaseOrderIsReleased": False,
                "ReleaseIsNotCompleted": True,
            }
        )
    }
    out = S4HANAClient(_settings()).get_purchase_order_approval_status("4500001234")
    summary = out["approvalSummary"]
    assert summary["isReleased"] is False
    assert summary["releaseIncomplete"] is True
    # the named next approver / workflow step list has no connected source
    assert summary["namedApproverAvailable"] is False
    select = fake_s4.requests[-1]["params"]["$select"].split(",")
    assert "PurchaseOrderIsReleased" in select and "ReleaseIsNotCompleted" in select


def test_purchase_order_approval_status_none_when_po_missing(fake_s4):
    assert S4HANAClient(_settings()).get_purchase_order_approval_status("4500000000") is None


def test_goods_receipts_for_po_filters_by_po_and_flags_active_receipt(fake_s4):
    fake_s4.routes["/A_MaterialDocumentItem"] = {
        "json": _d(
            [
                {"MaterialDocument": "5000000001", "GoodsMovementType": "101", "GoodsMovementIsCancelled": False},
                {"MaterialDocument": "5000000002", "GoodsMovementType": "102", "GoodsMovementIsCancelled": True},
            ]
        )
    }
    out = S4HANAClient(_settings()).get_goods_receipts_for_po("4500001234")
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "PurchaseOrder eq '4500001234'"
    assert params["$orderby"] == "PostingDate desc"
    assert out["goodsReceiptItemCount"] == 2
    assert out["hasActiveGoodsReceipt"] is True


def test_goods_receipts_for_po_none_when_no_material_docs(fake_s4):
    fake_s4.routes["/A_MaterialDocumentItem"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_goods_receipts_for_po("4500001234") is None


def test_goods_receipts_all_cancelled_reports_no_active_receipt(fake_s4):
    fake_s4.routes["/A_MaterialDocumentItem"] = {
        "json": _d([{"MaterialDocument": "5000000002", "GoodsMovementIsCancelled": True}])
    }
    out = S4HANAClient(_settings()).get_goods_receipts_for_po("4500001234")
    assert out["hasActiveGoodsReceipt"] is False


def test_invoice_items_filter_pins_invoice_and_fiscal_year_and_strips_bank(fake_s4):
    fake_s4.routes["/A_SuplrInvcItemPurOrdRef"] = {
        "json": _d(
            [
                {
                    "SupplierInvoice": "5105601234",
                    "FiscalYear": "2026",
                    "SupplierInvoiceItem": "1",
                    "PurchaseOrder": "4500001234",
                    "IBAN": "DE89370400440532013000",  # must never survive
                }
            ]
        )
    }
    rows = S4HANAClient(_settings()).get_invoice_items("5105601234", "2026")
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "SupplierInvoice eq '5105601234' and FiscalYear eq '2026'"
    assert rows[0]["PurchaseOrder"] == "4500001234"
    assert "IBAN" not in rows[0]


def test_capability_falls_through_to_next_candidate_when_first_is_absent(fake_s4):
    # supplier_invoice_item candidate 1 is A_SuplrInvcItemPurOrdRef; make that a
    # hard 404 (not activated) and serve candidate 2's un-abbreviated name.
    fake_s4.routes["/A_SuplrInvcItemPurOrdRef"] = {"status": 404, "text": "<html>not found</html>"}
    fake_s4.routes["/A_SupplierInvoiceItemPurOrdReference"] = {
        "json": _d([{"SupplierInvoice": "5105601234", "PurchaseOrder": "4500001234"}])
    }
    rows = S4HANAClient(_settings()).get_invoice_items("5105601234", "2026")
    assert rows[0]["PurchaseOrder"] == "4500001234"
    assert "A_SupplierInvoiceItemPurOrdReference" in fake_s4.requests[-1]["url"]


def test_capability_falls_through_when_first_service_returns_403(fake_s4):
    # API_PURCHASE_REQUISITION_SRV not in the tenant's communication arrangement
    # -> 403; the leaner API_PURCHASEREQ_PROCESS_SRV is. Same entity-set name, so
    # route by service prefix in the URL.
    fake_s4.routes["/API_PURCHASE_REQUISITION_SRV/A_PurchaseRequisitionHeader"] = {
        "status": 403, "json": {"error": {"message": {"value": "Service not authorized"}}},
    }
    fake_s4.routes["/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionHeader("] = {
        "json": _d({"PurchaseRequisition": "10000000", "PurchaseRequisitionType": "NB"}),
    }
    fake_s4.routes["/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionItem"] = {"json": _d([])}
    fake_s4.routes["/API_PURCHASE_REQUISITION_SRV/A_PurchaseRequisitionItem"] = {
        "status": 403, "json": {"error": {"message": {"value": "Service not authorized"}}},
    }

    out = S4HANAClient(_settings()).get_purchase_requisition_status("10000000")
    assert out["PurchaseRequisition"] == "10000000"
    assert "API_PURCHASEREQ_PROCESS_SRV" in fake_s4.requests[-1]["url"]


def test_unknown_property_400_is_classified_absent_and_capability_fails_over(fake_s4):
    # S43 on-prem: API_JOURNALENTRYITEMBASIC_SRV answers, but its
    # A_JournalEntryItemBasic type has no AccountingDocument property, so even
    # the probe 400s with "Property 'AccountingDocument' not found in type
    # '…A_JournalEntryItemBasicType'". That must count as "absent" (try the next
    # candidate), not "ok".
    fake_s4.routes["/API_JOURNALENTRYITEMBASIC_SRV/A_JournalEntryItemBasic"] = {
        "status": 400,
        "json": {"error": {"message": {"value": (
            "Property 'AccountingDocument' not found in type "
            "'com.sap.gateway.srvd_a2x…A_JournalEntryItemBasicType'"
        )}}},
    }
    fake_s4.routes["/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"] = {
        "json": _d([{"AccountingDocument": "1900000001", "ClearingDate": "/Date(1719360000000)/"}]),
    }
    out = S4HANAClient(_settings()).get_payment_clearing_status("1900000001", "2017", "1710")
    assert out is not None and out["isCleared"] is True
    assert "API_OPLACCTGDOCITEMCUBE_SRV" in fake_s4.requests[-1]["url"]


def test_journal_entry_fails_over_when_probe_ok_but_filtered_query_400s(fake_s4):
    # Real-world shape: the bare $top=1 probe on API_JOURNALENTRYITEMBASIC_SRV
    # succeeds (no $select / $filter), so the catalogue locks onto it — then the
    # actual clearing query 400s because AccountingDocument (a $filter key) is
    # not on this build's entity type. The agent must fail over, not dead-end.
    fake_s4.routes["/API_JOURNALENTRYITEMBASIC_SRV/A_JournalEntryItemBasic"] = {
        "reject_field": "AccountingDocument",
        "reject_status": 400,
        "reject_message": (
            "Property 'AccountingDocument' not found in type "
            "'com.sap…A_JournalEntryItemBasicType'"
        ),
        "json": _d([]),  # the bare probe (no $select/$filter) sees this
    }
    fake_s4.routes["/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"] = {
        "json": _d([{"AccountingDocument": "1900000001", "ClearingDate": "/Date(1719360000000)/"}]),
    }
    out = S4HANAClient(_settings()).get_payment_clearing_status("1900000001", "2017", "1710")
    assert out is not None and out["isCleared"] is True
    assert "API_OPLACCTGDOCITEMCUBE_SRV" in fake_s4.requests[-1]["url"]


def test_all_journal_entry_candidates_missing_field_degrades_cleanly(fake_s4):
    # If no journal-entry candidate models AccountingDocument, the lookup fails
    # with a clear "has no resource/segment" error the tool layer can relay —
    # it does not raise something opaque or loop forever.
    for svc, ent in (
        ("API_JOURNALENTRYITEMBASIC_SRV", "A_JournalEntryItemBasic"),
        ("API_OPLACCTGDOCITEMCUBE_SRV", "A_OperationalAcctgDocItemCube"),
        ("API_JOURNAL_ENTRY_SRV", "A_JournalEntryItem"),
    ):
        fake_s4.routes[f"/{svc}/{ent}"] = {
            "reject_field": "AccountingDocument",
            "reject_status": 400,
            "reject_message": f"Property 'AccountingDocument' not found in type '…{ent}Type'",
            "json": _d([]),
        }
    with pytest.raises(S4HANAError, match="has no resource/segment 'AccountingDocument'"):
        S4HANAClient(_settings()).get_payment_clearing_status("1900000001", "2017", "1710")


def test_403_error_names_the_destination_user_authorization(fake_s4):
    fake_s4.routes["/A_PurchaseOrderScheduleLine"] = {
        "status": 403, "json": {"error": {"message": {"value": "Forbidden"}}},
    }
    with pytest.raises(S4HANAError, match="not authorised for this OData service"):
        S4HANAClient(_settings()).get_purchase_order_delivery_schedule("4500000030")


def test_401_arms_cooldown_and_short_circuits_further_calls(fake_s4):
    fake_s4.routes["/"] = {"status": 401, "json": {"error": {"message": {"value": "Logon failed"}}}}
    client = S4HANAClient(_settings())

    # get_purchase_order_status makes two internal _get() calls for an
    # uncached capability (a catalogue probe, then the real fetch); the probe
    # already 401s and arms the cooldown, so the real fetch is itself
    # short-circuited -- only one HTTP request actually reaches S/4HANA.
    with pytest.raises(S4HANAError):
        client.get_purchase_order_status("4500000030")
    calls_after_first = len(fake_s4.requests)
    assert calls_after_first == 1  # one real attempt against S/4HANA

    # A second, unrelated lookup within the cooldown must NOT hit S/4HANA again
    # (it would just be another failed logon against the tenant) -- and gets a
    # distinct, lighter "retry shortly" message rather than the raw 401 again.
    with pytest.raises(S4HANAError, match="retry in a few seconds"):
        client.get_vendor_details("1000000")
    assert len(fake_s4.requests) == calls_after_first  # no new HTTP call


def test_401_cooldown_expires_and_retries_s4hana(fake_s4):
    fake_s4.routes["/"] = {"status": 401, "json": {"error": {"message": {"value": "Logon failed"}}}}
    client = S4HANAClient(_settings())
    with pytest.raises(S4HANAError):
        client.get_purchase_order_status("4500000030")
    assert len(fake_s4.requests) == 1

    client._auth_broken_until = time.monotonic() - 1  # simulate cooldown elapsed
    with pytest.raises(S4HANAError):
        client.get_purchase_order_status("4500000030")
    assert len(fake_s4.requests) == 2  # tried S/4HANA again


def test_probe_catalog_makes_one_real_call_when_destination_401s(fake_s4):
    fake_s4.routes["/"] = {"status": 401, "json": {"error": {"message": {"value": "Logon failed"}}}}
    out = S4HANAClient(_settings()).probe_catalog()
    assert all(v["ok"] is False for v in out.values())
    # ~20 candidates across the catalogue, but only the first probe should have
    # actually reached S/4HANA -- the rest are short-circuited by the cooldown.
    assert len(fake_s4.requests) == 1


def test_probe_classifies_status_codes(fake_s4):
    c = S4HANAClient(_settings())
    fake_s4.routes["/S_OK"] = {"json": _d([])}
    fake_s4.routes["/S_403"] = {"status": 403, "json": {"error": {"message": {"value": "forbidden"}}}}
    fake_s4.routes["/S_400"] = {"status": 400, "json": {"error": {"message": {"value": "bad $top"}}}}
    fake_s4.routes["/S_500"] = {"status": 500, "json": {"error": {"message": {"value": "boom"}}}}
    assert c._probe("SRV", "S_OK") == "ok"
    assert c._probe("SRV", "S_403") == "absent"      # not authorised here -> try next candidate
    assert c._probe("SRV", "S_400") == "ok"          # service is fine, our probe query was rejected
    assert c._probe("SRV", "S_500") == "unreachable"  # tells us nothing, do not lock


def test_capability_resolution_is_cached_per_client(fake_s4):
    fake_s4.routes["/A_JournalEntryItemBasic"] = {"json": _d([])}
    c = S4HANAClient(_settings())
    c.get_payment_clearing_status("1400000123", "2026", "1710")
    probes_after_first = sum(1 for r in fake_s4.requests if r["params"].get("$top") == "1")
    c.get_payment_clearing_status("1400000124", "2026", "1710")
    probes_after_second = sum(1 for r in fake_s4.requests if r["params"].get("$top") == "1")
    # the candidate was probed once and then reused — no second probe
    assert probes_after_first == 1
    assert probes_after_second == 1


def test_probe_catalog_reports_every_capability(fake_s4):
    fake_s4.routes["/"] = {"json": _d([])}  # anything answers -> every candidate "ok"
    out = S4HANAClient(_settings()).probe_catalog()
    assert set(out) == {
        "supplier_invoice_header", "supplier_invoice_item", "journal_entry_item",
        "purchase_order_header", "purchase_order_item", "purchase_order_schedule_line",
        "goods_receipt_item", "purchase_requisition_header", "purchase_requisition_item",
        "business_partner", "supplier", "budget",
        "business_partner_address", "address_email", "business_partner_bank",
    }
    assert all(v["ok"] for v in out.values())
    assert out["supplier_invoice_item"]["resolved"].endswith("A_SuplrInvcItemPurOrdRef")


def test_probe_catalog_marks_capability_unresolved_when_all_candidates_absent(fake_s4):
    # every request 404s hard -> "absent"
    fake_s4.routes["/"] = {"status": 404, "text": "nope"}
    out = S4HANAClient(_settings()).probe_catalog()
    assert out["goods_receipt_item"]["ok"] is False
    assert out["goods_receipt_item"]["resolved"] is None
    assert [c["status"] for c in out["goods_receipt_item"]["candidates"]] == ["absent", "absent", "absent"]


def _cleared_je_row(doc, **over):
    row = {
        "AccountingDocument": doc, "FiscalYear": "2017", "CompanyCode": "1710",
        "ClearingDate": "/Date(1600000000000)/", "ClearingJournalEntry": "2000000055",
    }
    row.update(over)
    return row


def test_invoice_payment_status_uses_header_accounting_document_when_present(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "CompanyCode": "1710",
            "SupplierInvoiceStatus": "5", "AccountingDocument": "5100000016",
            "PaymentBlockingReason": "",
        }])
    }
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {"json": _d([_cleared_je_row("5100000016")])}

    out = S4HANAClient(_settings()).get_invoice_payment_status("5100000016")
    assert out["accountingDocument"] == "5100000016"
    assert out["accountingDocumentSource"] == "invoice_header"
    assert out["paymentCleared"] is True
    assert out["clearingJournalEntry"] == "2000000055"
    ps = out["paymentSummary"]
    assert ps["paymentDocument"] == "2000000055"
    assert "F110" in ps["runIdNote"]


def test_payment_summary_pulls_house_bank_and_method_from_clearing_doc(fake_s4):
    # the invoice's cleared line points at clearing doc 2000000055; that doc's
    # bank line carries the payment method / house bank / amount.
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "CompanyCode": "1710",
            "SupplierInvoiceStatus": "5", "AccountingDocument": "5100000016",
        }])
    }
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {
        "json": _d([
            _cleared_je_row("5100000016"),  # the invoice line
            _cleared_je_row("2000000055", AccountingDocument="2000000055",
                            HouseBank="HB01", HouseBankAccount="GIRO",
                            PaymentMethod="T", AmountInCompanyCodeCurrency="212652.32",
                            CompanyCodeCurrency="USD"),  # the payment (clearing) doc's bank line
        ])
    }
    out = S4HANAClient(_settings()).get_invoice_payment_status("5100000016")
    ps = out["paymentSummary"]
    assert ps["houseBank"] == "HB01"
    assert ps["houseBankAccount"] == "GIRO"
    assert ps["paymentMethod"] == "T"
    assert ps["paymentAmount"] == "212652.32"
    assert ps["paymentCurrency"] == "USD"


def test_invoice_payment_status_resolves_fi_document_via_journal_entry_reference(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "CompanyCode": "1710",
            "SupplierInvoiceStatus": "5",  # no AccountingDocument in this build
        }])
    }
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {"json": _d([_cleared_je_row("4900000200")])}

    out = S4HANAClient(_settings()).get_invoice_payment_status("5100000016")
    assert out["accountingDocument"] == "4900000200"
    assert out["accountingDocumentSource"] == "journal_entry_reference"
    assert out["paymentCleared"] is True


def test_invoice_payment_status_falls_back_to_assuming_same_number(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "CompanyCode": "1710",
            "SupplierInvoiceStatus": "5",
        }])
    }
    fake_s4.routes["/A_JournalEntryItemBasic"] = {"json": _d([])}  # nothing matches, ever

    out = S4HANAClient(_settings()).get_invoice_payment_status("5100000016")
    assert out["accountingDocument"] == "5100000016"
    assert out["accountingDocumentSource"] == "assumed_equal_to_invoice_number"
    assert out["paymentCleared"] is None
    assert out["clearingDetailsFound"] is False
    assert "confirm the FI document" in out["note"]


def test_journal_entry_item_prefers_operational_cube_falls_back_to_basic(fake_s4):
    # API_OPLACCTGDOCITEMCUBE_SRV is tried first for journal_entry_item; when it
    # is not activated here, resolution falls over to API_JOURNALENTRYITEMBASIC_SRV.
    fake_s4.routes["/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"] = {
        "status": 404, "text": "<html>not found</html>",
    }
    fake_s4.routes["/A_JournalEntryItemBasic"] = {"json": _d([_cleared_je_row("1400000123")])}

    out = S4HANAClient(_settings()).get_payment_clearing_status("1400000123", "2026", "1710")
    assert out["isCleared"] is True
    assert "API_JOURNALENTRYITEMBASIC_SRV" in fake_s4.requests[-1]["url"]


def test_invoice_payment_status_none_when_invoice_missing(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_invoice_payment_status("9999999999") is None


def test_purchase_order_items_filter_and_orderby(fake_s4):
    fake_s4.routes["/A_PurchaseOrderItem"] = {
        "json": _d([{"PurchaseOrder": "4500000001", "PurchaseOrderItem": "10", "OrderQuantity": "100"}])
    }
    rows = S4HANAClient(_settings()).get_purchase_order_items("4500000001")
    params = fake_s4.requests[-1]["params"]
    assert params["$filter"] == "PurchaseOrder eq '4500000001'"
    assert params["$orderby"] == "PurchaseOrderItem asc"
    assert rows[0]["OrderQuantity"] == "100"


def test_purchase_order_delivery_schedule_summarises_dates(fake_s4):
    fake_s4.routes["/A_PurchaseOrderScheduleLine"] = {
        "json": _d([
            {"PurchaseOrder": "4500000001", "PurchaseOrderItem": "10", "ScheduleLine": "1",
             "ScheduleLineDeliveryDate": "2017-03-15T00:00:00", "ScheduleLineOrderQuantity": "60"},
            {"PurchaseOrder": "4500000001", "PurchaseOrderItem": "10", "ScheduleLine": "2",
             "ScheduleLineDeliveryDate": "2017-02-01T00:00:00", "ScheduleLineOrderQuantity": "40"},
        ])
    }
    out = S4HANAClient(_settings()).get_purchase_order_delivery_schedule("4500000001")
    assert fake_s4.requests[-1]["params"]["$filter"] == "PurchaseOrder eq '4500000001'"
    assert out["scheduleLineCount"] == 2
    assert out["earliestDeliveryDate"] == "2017-02-01T00:00:00"
    assert out["latestDeliveryDate"] == "2017-03-15T00:00:00"


def test_purchase_order_delivery_schedule_none_when_no_lines(fake_s4):
    fake_s4.routes["/A_PurchaseOrderScheduleLine"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_purchase_order_delivery_schedule("4500000001") is None


def test_purchase_order_delivery_schedule_raises_when_subnode_absent(fake_s4):
    # No route registered -> fake returns a 404 OData error body -> _get returns
    # None -> treated as "no lines". A hard ICF 404 (text body) would raise; make
    # sure that surfaces rather than being swallowed.
    fake_s4.routes["/A_PurchaseOrderScheduleLine"] = {"status": 404, "text": "<html>not found</html>"}
    with pytest.raises(S4HANAError):
        S4HANAClient(_settings()).get_purchase_order_delivery_schedule("4500000001")


def test_check_three_way_match_computes_price_and_quantity_variance(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "CompanyCode": "1710",
            "SupplierInvoiceStatus": "5", "InvoiceGrossAmount": "11000", "DocumentCurrency": "USD",
            "PaymentBlockingReason": "P",
        }])
    }
    fake_s4.routes["/A_SuplrInvcItemPurOrdRef"] = {
        "json": _d([{
            "SupplierInvoice": "5100000016", "FiscalYear": "2017", "SupplierInvoiceItem": "1",
            "PurchaseOrder": "4500000001", "PurchaseOrderItem": "10",
            "QuantityInPurchaseOrderUnit": "100", "PurchaseOrderQuantityUnit": "EA",
            "SupplierInvoiceItemAmount": "11000", "DocumentCurrency": "USD",
        }])
    }
    fake_s4.routes["/A_PurchaseOrderItem"] = {
        "json": _d([{
            "PurchaseOrder": "4500000001", "PurchaseOrderItem": "00010",
            "OrderQuantity": "100", "NetPriceAmount": "100", "NetPriceQuantity": "1",
            "PurchaseOrderQuantityUnit": "EA", "DocumentCurrency": "USD",
        }])
    }
    fake_s4.routes["/A_MaterialDocumentItem"] = {
        "json": _d([{
            "MaterialDocument": "5000000001", "PurchaseOrder": "4500000001",
            "PurchaseOrderItem": "10", "QuantityInEntryUnit": "90", "EntryUnit": "EA",
            "DebitCreditCode": "S", "GoodsMovementIsCancelled": False,
        }])
    }
    out = S4HANAClient(_settings()).check_three_way_match("5100000016", "2017")
    assert out["isBlockedForPayment"] is True
    assert out["paymentBlockingReason"] == "P"
    assert out["purchaseOrders"] == ["4500000001"]

    line = out["lineComparisons"][0]
    assert line["matchedToPurchaseOrderItem"] is True
    assert line["invoicedUnitPrice"] == 110.0
    assert line["purchaseOrderUnitPrice"] == 100.0
    assert line["priceVariancePerUnit"] == 10.0
    assert line["priceVariancePercent"] == 10.0
    assert line["goodsReceiptQuantity"] == 90.0
    assert line["quantityVarianceVsGoodsReceipt"] == 10.0
    assert line["hasPriceVariance"] is True
    assert line["hasQuantityVariance"] is True

    summary = out["computedMatchSummary"]
    assert summary["priceVarianceDetected"] is True
    assert summary["quantityVarianceDetected"] is True
    assert summary["linesWithoutPurchaseOrderItemMatch"] == 0


def test_check_three_way_match_returns_none_when_invoice_missing(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {"json": _d([])}
    assert S4HANAClient(_settings()).check_three_way_match("9999999999", "2017") is None


def test_check_three_way_match_no_variance_when_all_three_agree(fake_s4):
    fake_s4.routes["/A_SupplierInvoice"] = {
        "json": _d([{
            "SupplierInvoice": "5100000020", "FiscalYear": "2017",
            "SupplierInvoiceStatus": "5", "PaymentBlockingReason": "",
        }])
    }
    fake_s4.routes["/A_SuplrInvcItemPurOrdRef"] = {
        "json": _d([{
            "SupplierInvoice": "5100000020", "FiscalYear": "2017", "SupplierInvoiceItem": "1",
            "PurchaseOrder": "4500000002", "PurchaseOrderItem": "10",
            "QuantityInPurchaseOrderUnit": "50", "SupplierInvoiceItemAmount": "5000",
        }])
    }
    fake_s4.routes["/A_PurchaseOrderItem"] = {
        "json": _d([{
            "PurchaseOrder": "4500000002", "PurchaseOrderItem": "10",
            "OrderQuantity": "50", "NetPriceAmount": "100", "NetPriceQuantity": "1",
        }])
    }
    fake_s4.routes["/A_MaterialDocumentItem"] = {
        "json": _d([{
            "MaterialDocument": "5000000002", "PurchaseOrder": "4500000002",
            "PurchaseOrderItem": "10", "QuantityInEntryUnit": "50", "DebitCreditCode": "S",
        }])
    }
    out = S4HANAClient(_settings()).check_three_way_match("5100000020", "2017")
    assert out["isBlockedForPayment"] is False
    assert out["computedMatchSummary"]["priceVarianceDetected"] is False
    assert out["computedMatchSummary"]["quantityVarianceDetected"] is False


def test_budget_status_is_typed_not_connected_when_no_budget_service(fake_s4):
    fake_s4.routes["/"] = {"status": 404, "text": "not found"}  # every budget probe misses
    out = S4HANAClient(_settings()).get_budget_status("purchase_order", "4500001234", "2026")
    assert out["budgetAvailable"] is False
    assert out["detectedBudgetService"] is None
    assert out["costObject"] == "4500001234"
    assert "FMAVCR01" in out["handoff"]
    assert "estimate" in out["handoff"].lower()


def test_budget_status_notes_a_detected_budget_service(fake_s4):
    fake_s4.routes["/A_BudgetEntryDocument"] = {"json": _d([])}
    fake_s4.routes["/"] = {"status": 404, "text": "x"}
    out = S4HANAClient(_settings()).get_budget_status("cost_center", "1000")
    assert out["budgetAvailable"] is False  # detected but fields not mapped
    assert out["detectedBudgetService"] == "API_BUDGET_ENTRY_DOCUMENT_SRV/A_BudgetEntryDocument"
    assert "not yet mapped" in out["reason"]


def test_get_cost_object_actuals_returns_none_for_unknown_type(fake_s4):
    assert S4HANAClient(_settings()).get_cost_object_actuals("widget", "123") is None


def test_get_cost_object_actuals_none_when_no_postings(fake_s4):
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_cost_object_actuals("internal_order", "700123") is None


def test_get_cost_object_actuals_filters_by_order_id_for_internal_order(fake_s4):
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {
        "json": _d([{
            "OrderID": "700123", "AmountInCompanyCodeCurrency": "100.00",
            "DebitCreditCode": "S", "CompanyCodeCurrency": "USD",
        }])
    }
    out = S4HANAClient(_settings()).get_cost_object_actuals("internal_order", "700123", "2026")
    assert out["netPostedAmount"] == 100.0
    assert "OrderID eq '700123'" in fake_s4.requests[-1]["params"]["$filter"]
    assert "FiscalYear eq '2026'" in fake_s4.requests[-1]["params"]["$filter"]


def test_budget_status_computes_actual_spend_from_cost_center_postings(fake_s4):
    fake_s4.routes["/A_OperationalAcctgDocItemCube"] = {
        "json": _d([
            {"CostCenter": "1000", "FiscalYear": "2026", "AmountInCompanyCodeCurrency": "5000.00",
             "CompanyCodeCurrency": "USD", "DebitCreditCode": "S"},
            {"CostCenter": "1000", "FiscalYear": "2026", "AmountInCompanyCodeCurrency": "1200.00",
             "CompanyCodeCurrency": "USD", "DebitCreditCode": "H"},
        ])
    }
    fake_s4.routes["/"] = {"status": 404, "text": "no budget service"}
    out = S4HANAClient(_settings()).get_budget_status("cost_center", "1000", "2026")
    assert out["budgetAvailable"] is False
    actuals = out["actualSpend"]
    assert actuals["netPostedAmount"] == 3800.0  # 5000 debit - 1200 credit
    assert actuals["currency"] == "USD"
    assert actuals["postingCount"] == 2
    assert "actualSpend / commitmentValue" in out["reason"]
    assert out["commitmentValue"] is None  # only computed for purchase_order


def test_budget_status_computes_po_commitment_value(fake_s4):
    fake_s4.routes["/A_PurchaseOrderItem"] = {
        "json": _d([
            {"PurchaseOrder": "4500000030", "PurchaseOrderItem": "10",
             "NetAmount": "10000.00", "DocumentCurrency": "USD"},
            {"PurchaseOrder": "4500000030", "PurchaseOrderItem": "20",
             "NetAmount": "2500.00", "DocumentCurrency": "USD"},
        ])
    }
    fake_s4.routes["/"] = {"status": 404, "text": "no other data"}
    out = S4HANAClient(_settings()).get_budget_status("purchase_order", "4500000030")
    commitment = out["commitmentValue"]
    assert commitment["committedValue"] == 12500.0
    assert commitment["currency"] == "USD"
    assert commitment["lineCount"] == 2


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


def test_vendor_email_addresses_returns_company_and_contact_emails(fake_s4):
    # deliberate PII exception: unlike every other lookup, EmailAddress must
    # survive here.
    fake_s4.routes["/A_BusinessPartnerAddress"] = {
        "json": _d([{"BusinessPartner": "1000000", "AddressID": "23421"}])
    }
    fake_s4.routes["/A_AddressEmailAddress"] = {
        "json": _d([
            {"AddressID": "23421", "Person": "", "OrdinalNumber": "1",
             "EmailAddress": "info@17100001.com", "IsDefaultEmailAddress": True},
            {"AddressID": "23421", "Person": "23437", "OrdinalNumber": "1",
             "EmailAddress": "james.smith@17100001.com", "IsDefaultEmailAddress": True},
        ])
    }
    out = S4HANAClient(_settings()).get_vendor_email_addresses("1000000")
    assert len(out) == 2
    company = next(r for r in out if r["contactPerson"] is None)
    assert company["emailAddress"] == "info@17100001.com"
    assert company["isDefault"] is True
    contact = next(r for r in out if r["contactPerson"] == "23437")
    assert contact["emailAddress"] == "james.smith@17100001.com"

    # every other lookup still strips EmailAddress -- the exception is scoped
    from ahf_finance_agent.guardrails import SENSITIVE_KEYS
    assert "emailaddress" in SENSITIVE_KEYS


def test_vendor_email_addresses_none_when_no_address(fake_s4):
    fake_s4.routes["/A_BusinessPartnerAddress"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_vendor_email_addresses("9999999") is None


def test_vendor_bank_accounts_returns_iban_and_swift_unstripped(fake_s4):
    # deliberate PII/fraud-risk exception: unlike every other lookup, bank
    # fields must survive here.
    fake_s4.routes["/A_BusinessPartnerBank"] = {
        "json": _d([{
            "BusinessPartner": "1000000", "BankIdentification": "0001",
            "BankCountryKey": "US", "BankName": "Bank 1 - SAMPLE BANK", "BankNumber": "123456789",
            "BankAccount": "000111222333", "IBAN": "DE89370400440532013000",
            "SWIFTCode": "COBADEFFXXX", "BankAccountHolderName": "USSU-VSF04",
            "CityName": "Palo Alto",
        }])
    }
    out = S4HANAClient(_settings()).get_vendor_bank_accounts("1000000")
    assert len(out) == 1
    account = out[0]
    assert account["bankName"] == "Bank 1 - SAMPLE BANK"
    assert account["iban"] == "DE89370400440532013000"
    assert account["swiftCode"] == "COBADEFFXXX"
    assert account["bankAccount"] == "000111222333"
    assert account["bankAccountHolderName"] == "USSU-VSF04"
    assert account["cityName"] == "Palo Alto"

    # every other lookup still strips these fields -- the exception is scoped
    from ahf_finance_agent.guardrails import SENSITIVE_KEYS
    assert {"bankaccount", "iban", "swiftcode", "bankaccountholdername"} <= SENSITIVE_KEYS


def test_vendor_bank_accounts_normalises_unpopulated_fields_to_none(fake_s4):
    # Live tenant leaves unpopulated bank fields as "" rather than omitting
    # them (e.g. a US vendor's IBAN, or a DE vendor's routing BankAccount).
    fake_s4.routes["/A_BusinessPartnerBank"] = {
        "json": _d([
            {
                "BusinessPartner": "17100001", "BankIdentification": "0001",
                "BankCountryKey": "US", "BankName": "Bank 1 - SAMPLE BANK",
                "BankNumber": "011000390", "SWIFTCode": "BOFAUS2M",
                "BankAccountHolderName": "", "BankAccountName": "",
                "IBAN": "", "BankAccount": "102030", "BankControlKey": "",
                "CityName": "Palo Alto",
            },
            {
                "BusinessPartner": "10100001", "BankIdentification": "0001",
                "BankCountryKey": "DE", "BankName": "Bank 3 - SAMPLE BANK",
                "BankNumber": "23030000", "SWIFTCode": "HYVEDEMM237",
                "BankAccountHolderName": "", "BankAccountName": "",
                "IBAN": "DE21230300000001230003", "BankAccount": "", "BankControlKey": "",
                "CityName": "23510 Lübeck",
            },
        ])
    }
    out = S4HANAClient(_settings()).get_vendor_bank_accounts("17100001")
    us_row, de_row = out
    assert us_row["iban"] is None
    assert us_row["bankAccount"] == "102030"
    assert us_row["bankAccountHolderName"] is None
    assert de_row["iban"] == "DE21230300000001230003"
    assert de_row["bankAccount"] is None


def test_vendor_bank_accounts_none_when_no_account_on_file(fake_s4):
    fake_s4.routes["/A_BusinessPartnerBank"] = {"json": _d([])}
    assert S4HANAClient(_settings()).get_vendor_bank_accounts("9999999") is None


def test_ping_hits_business_partner_with_top_1(fake_s4):
    fake_s4.routes["/A_BusinessPartner"] = {"json": _d([{"BusinessPartner": "1"}])}
    out = S4HANAClient(_settings()).ping()
    assert out == {"reachable": True, "rows": 1}
    assert fake_s4.requests[-1]["params"]["$top"] == "1"
    assert fake_s4.requests[-1]["params"]["$select"] == "BusinessPartner"
