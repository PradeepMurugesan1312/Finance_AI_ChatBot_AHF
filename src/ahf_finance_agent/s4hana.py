"""Read-only S/4HANA access through the ``S43`` BTP destination.

Build step 3. Six lookups over five standard SAP S/4HANA Cloud OData v2 APIs,
every one a ``GET``:

===========================  =========================================  ============================
Lookup                       OData service                              Entity set
===========================  =========================================  ============================
invoice status / by vendor   ``API_SUPPLIERINVOICE_PROCESS_SRV``        ``A_SupplierInvoice``
payment clearing             ``API_JOURNALENTRYITEMBASIC_SRV``          ``A_JournalEntryItemBasic``
purchase order status        ``API_PURCHASEORDER_PROCESS_SRV``          ``A_PurchaseOrder``
purchase requisition status  ``API_PURCHASEREQ_PROCESS_SRV``            ``A_PurchaseRequisitionHeader`` / ``…Item``
vendor / business partner    ``API_BUSINESS_PARTNER``                   ``A_BusinessPartner`` / ``A_Supplier``
===========================  =========================================  ============================

Design constraints from the brief and :mod:`ahf_finance_agent.guardrails`:

* **Structurally read-only.** This module issues ``GET`` only. There is no
  ``post`` / ``patch`` / ``delete`` path, by construction.
* **No PII / bank data out.** Each call sends a curated ``$select`` that never
  asks for a bank, tax, or personal field, and every row is additionally run
  through :func:`strip_sensitive_keys` before it leaves this module.
* A missing record (an OData 404 with an ``{"error": …}`` body, or an empty
  result set) is a normal answer — it returns ``None`` / ``[]``, not an
  exception. Anything else that stops us getting data — including a bare 404
  from the ICF layer (wrong service root / inactive service) — raises
  :class:`S4HANAError`, which the tool layer turns into an ``{"error": …}``
  message the model can act on.

The service / entity-set / ``$select`` names live in module constants so a
tenant-specific correction is a one-line change.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Any

import httpx

from ahf_finance_agent.btp import resolve_destination
from ahf_finance_agent.btp.destinations import DestinationError
from ahf_finance_agent.config import Settings, get_settings
from ahf_finance_agent.guardrails import strip_sensitive_keys

logger = logging.getLogger(__name__)


class S4HANAError(RuntimeError):
    """Any failure reaching or reading data from S/4HANA (not 'no such record')."""


class _UnknownODataSegment(Exception):
    """Gateway 404 'Resource not found for the segment X' — a bad $select field
    (release-specific) or a bad entity set. Carries the offending name."""

    def __init__(self, segment: str) -> None:
        super().__init__(segment)
        self.segment = segment


_BAD_SEGMENT_RE = re.compile(r"not found for the segment '([^']+)'", re.I)


# --- service catalogue ----------------------------------------------------
# S/4HANA Cloud public OData v2 APIs. Adjust here if a tenant exposes a
# different service alias or custom projection.

_INVOICE_SRV = "API_SUPPLIERINVOICE_PROCESS_SRV"
_INVOICE_SET = "A_SupplierInvoice"
# NOTE: "IsPaid" and "AccountingDocument" were dropped — both are absent from
# the on-premise build of this service in the POC landscape and Gateway 404s
# the whole request on an unknown $select field ("Resource not found for the
# segment 'X'"). _select_get() drops any further release-specific gap at
# runtime; this list is trimmed to fields verified present to avoid a wasted
# retry on every call.
_INVOICE_SELECT = (
    "SupplierInvoice", "FiscalYear", "CompanyCode", "DocumentDate", "PostingDate",
    "SupplierInvoiceStatus", "InvoicingParty", "InvoiceGrossAmount", "DocumentCurrency",
    "PaymentTerms", "DueCalculationBaseDate", "PaymentBlockingReason",
    "AccountingDocumentType", "ReverseDocument",
)

_PAYMENT_SRV = "API_JOURNALENTRYITEMBASIC_SRV"
_PAYMENT_SET = "A_JournalEntryItemBasic"
_PAYMENT_SELECT = (
    "CompanyCode", "FiscalYear", "AccountingDocument", "AccountingDocumentItem",
    "AccountingDocumentType", "PostingDate", "DocumentDate",
    "AmountInCompanyCodeCurrency", "CompanyCodeCurrency", "DebitCreditCode",
    "GLAccount", "Supplier", "ClearingDate", "ClearingJournalEntry",
    "ClearingJournalEntryFiscalYear",
)

_PO_SRV = "API_PURCHASEORDER_PROCESS_SRV"
_PO_SET = "A_PurchaseOrder"
_PO_SELECT = (
    "PurchaseOrder", "PurchaseOrderType", "CompanyCode", "PurchasingOrganization",
    "PurchasingGroup", "Supplier", "PurchaseOrderDate", "CreationDate",
    "PurchasingDocumentDeletionCode", "PurchaseOrderSubtype", "Language",
    "DocumentCurrency", "PurchaseOrderIsReleased", "ReleaseIsNotCompleted",
    "PurchasingCompletenessStatus", "PurchasingProcessingStatus",
)

_PR_SRV = "API_PURCHASEREQ_PROCESS_SRV"
_PR_HEADER_SET = "A_PurchaseRequisitionHeader"
_PR_HEADER_SELECT = (
    "PurchaseRequisition", "PurchaseRequisitionType", "CreationDate", "CreatedByUser",
    "PurchaseRequisitionForEdit",
)
_PR_ITEM_SET = "A_PurchaseRequisitionItem"
_PR_ITEM_SELECT = (
    "PurchaseRequisition", "PurchaseRequisitionItem", "PurchaseRequisitionItemText",
    "PurchaseReqnItemFirstDelivDate", "ProcessingStatus", "ReleaseStatusCode",
    "PurchaseRequisitionIsDeleted", "PurchaseRequisitionIsOnHold", "Plant",
    "MaterialGroup", "RequestedQuantity", "BaseUnit", "PurReqnReleaseStatus",
    "PurchaseOrder", "PurchasingDocument",
)

_BP_SRV = "API_BUSINESS_PARTNER"
_BP_SET = "A_BusinessPartner"
# Deliberately no bank / tax / personal-contact fields. Onboarding/setup facts only.
_BP_SELECT = (
    "BusinessPartner", "BusinessPartnerName", "BusinessPartnerFullName",
    "BusinessPartnerCategory", "BusinessPartnerGrouping", "BusinessPartnerType",
    "BusinessPartnerIsBlocked", "IsMarkedForArchiving", "CreationDate", "LastChangeDate",
)
_SUPPLIER_SET = "A_Supplier"
_SUPPLIER_SELECT = (
    "Supplier", "SupplierName", "SupplierAccountGroup", "CreationDate",
    "PurchasingIsBlockedForSupplier", "PostingIsBlocked", "DeletionIndicator",
    "IsNaturalPerson", "SupplierIsBlockedForPosting",
)


# --- helpers ------------------------------------------------------------

_SAFE_LITERAL = re.compile(r"^[\w./\- ]{1,60}$")


def _lit(value: str) -> str:
    """Quote a value for an OData v2 string literal, rejecting anything odd.

    Keys here are invoice / PO / PR / BP / company-code identifiers — short and
    alphanumeric. Rejecting anything else keeps ``$filter`` injection off the
    table rather than relying on escaping alone.
    """
    value = (value or "").strip()
    if not _SAFE_LITERAL.match(value):
        raise S4HANAError(f"refusing to query S/4HANA with suspicious identifier {value!r}")
    return "'" + value.replace("'", "''") + "'"


def _rows(body: Any) -> list[dict]:
    """Normalise an OData v2 (``{'d': {'results': [...]}}`` / ``{'d': {...}}``)
    or v4 (``{'value': [...]}`` ) body to a list of records."""
    if not body:
        return []
    if isinstance(body, dict) and isinstance(body.get("value"), list):  # v4
        return body["value"]
    d = body.get("d", body) if isinstance(body, dict) else body
    if isinstance(d, dict):
        if isinstance(d.get("results"), list):
            return d["results"]
        return [d]
    if isinstance(d, list):
        return d
    return []


def _clean(row: dict) -> dict:
    """Drop OData plumbing and any sensitive key that slipped into a projection."""
    out = {}
    for key, value in row.items():
        if key == "__metadata":
            continue
        if isinstance(value, dict) and "__deferred" in value:
            continue
        out[key] = value
    return strip_sensitive_keys(out)


def _is_set(value: Any) -> bool:
    """True when an OData date/id field carries a real value (not empty / epoch)."""
    if value in (None, "", 0):
        return False
    text = str(value)
    return text not in ("0", "/Date(0)/", "0000-00-00", "0000-00-00T00:00:00")


def _is_odata_error_body(resp: httpx.Response) -> bool:
    """True when a 4xx body is a SAP Gateway OData error (``{"error": {...}}``).

    A genuine "no such record" 404 carries one. A 404 from the ICF layer —
    wrong service root, or the OData service not activated — does not (it is an
    HTML error page), and must not be mistaken for an empty result.
    """
    if "json" not in resp.headers.get("content-type", "").lower():
        return False
    try:
        return isinstance(resp.json().get("error"), dict)
    except ValueError:
        return False


def _odata_error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        msg = err.get("message")
        if isinstance(msg, dict):
            msg = msg.get("value")
        if msg:
            return f"S/4HANA returned {resp.status_code}: {str(msg)[:200]}"
    except ValueError:
        pass
    return f"S/4HANA returned {resp.status_code}: {resp.text[:200]}"


# --- client -----------------------------------------------------------

class S4HANAClient:
    """Read-only OData client for the five in-scope S/4HANA services.

    Stateless between calls: each request re-resolves the ``S43`` destination
    (cheap — :func:`resolve_destination` caches the token until it nears
    expiry) and uses a fresh short-lived ``httpx.Client``, so a rotated token
    is never a problem.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    # -- transport ----------------------------------------------------
    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        s = self._s
        try:
            dest = resolve_destination(s.s4hana_destination_name)
        except DestinationError as exc:
            raise S4HANAError(
                f"Cannot resolve S/4HANA destination {s.s4hana_destination_name!r}: {exc}"
            ) from exc

        headers = dict(dest.headers)
        headers["Accept"] = "application/json"
        proxy = None
        if dest.proxy:
            proxy = httpx.Proxy(url=dest.proxy["url"], headers=dest.proxy.get("headers"))

        query = {"$format": "json", **(params or {})}
        # Prepend the OData service root (default /sap/opu/odata/sap) so the S43
        # destination can stay a bare host:port. dest.url may still carry it —
        # set S4HANA_ODATA_BASE_PATH="" then.
        root = s.s4hana_odata_base_path.strip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        url = f"/{root}{suffix}" if root else suffix
        try:
            with httpx.Client(
                base_url=dest.url,
                headers=headers,
                timeout=s.s4hana_timeout_seconds,
                proxy=proxy,
            ) as http:
                resp = http.get(url, params=query)
        except httpx.HTTPError as exc:
            raise S4HANAError(f"S/4HANA request to {url!r} failed: {exc}") from exc

        if resp.status_code == 404:
            if _is_odata_error_body(resp):
                seg = _BAD_SEGMENT_RE.search(resp.text)
                if seg:
                    raise _UnknownODataSegment(seg.group(1))
                logger.info("S/4HANA 404 (treated as 'no record'): %s", url)
                return None
            logger.error(
                "S/4HANA 404 with no OData error body: url=%s body=%s — likely a wrong "
                "service root (S4HANA_ODATA_BASE_PATH=%r) or an inactive OData service, "
                "not a missing record.",
                url, resp.text[:300], s.s4hana_odata_base_path,
            )
            raise S4HANAError(
                f"S/4HANA endpoint {url!r} returned 404 with no OData error body — check "
                f"S4HANA_ODATA_BASE_PATH ({s.s4hana_odata_base_path!r}) and that the OData "
                "service is activated."
            )
        if resp.is_error:
            logger.error("S/4HANA error: url=%s status=%s body=%s", url, resp.status_code, resp.text[:300])
            raise S4HANAError(_odata_error(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise S4HANAError(f"S/4HANA returned non-JSON ({resp.status_code}) for {url!r}") from exc

    def _select_get(self, path: str, select: tuple[str, ...], params: dict[str, str]) -> Any:
        """``_get`` with a self-healing ``$select``: if the service rejects a
        field ("Resource not found for the segment 'X'"), drop it and retry, so
        a release-specific field gap degrades to a smaller projection instead of
        failing the whole lookup. A rejected name that is not in ``$select``
        (e.g. the entity set itself) is a real error."""
        fields = list(select)
        while True:
            q = dict(params)
            if fields:
                q["$select"] = ",".join(fields)
            try:
                return self._get(path, q)
            except _UnknownODataSegment as exc:
                if exc.segment not in fields:
                    raise S4HANAError(
                        f"S/4HANA has no resource/segment {exc.segment!r} for {path!r}"
                    ) from exc
                fields.remove(exc.segment)
                logger.warning(
                    "S/4HANA service has no field %r on %s; retrying without it", exc.segment, path
                )

    def _entity(self, srv: str, entity_set: str, key_predicate: str, select: tuple[str, ...]) -> dict | None:
        body = self._select_get(f"/{srv}/{entity_set}({key_predicate})", select, {})
        rows = _rows(body)
        return _clean(rows[0]) if rows else None

    def _query(
        self, srv: str, entity_set: str, *, select: tuple[str, ...], filt: str, top: int = 20, orderby: str | None = None
    ) -> list[dict]:
        params = {"$filter": filt, "$top": str(max(1, min(top, 50)))}
        if orderby:
            params["$orderby"] = orderby
        return [_clean(r) for r in _rows(self._select_get(f"/{srv}/{entity_set}", select, params))]

    # -- lookups ----------------------------------------------------
    def get_invoice_status(self, invoice: str, fiscal_year: str) -> dict | None:
        # Addressed with $filter, not a read-by-key
        # A_SupplierInvoice(SupplierInvoice='..',FiscalYear='..'): several
        # on-premise Gateway builds of this service 404 the composite-key GET
        # while the collection query returns the row fine.
        rows = self._query(
            _INVOICE_SRV, _INVOICE_SET, select=_INVOICE_SELECT,
            filt=f"SupplierInvoice eq {_lit(invoice)} and FiscalYear eq {_lit(fiscal_year)}",
            top=1,
        )
        return rows[0] if rows else None

    def search_invoices_by_vendor(
        self, invoicing_party: str, company_code: str | None = None, *, top: int = 10
    ) -> list[dict]:
        filt = f"InvoicingParty eq {_lit(invoicing_party)}"
        if company_code:
            filt += f" and CompanyCode eq {_lit(company_code)}"
        return self._query(
            _INVOICE_SRV, _INVOICE_SET, select=_INVOICE_SELECT, filt=filt,
            top=top, orderby="PostingDate desc",
        )

    def get_payment_clearing_status(
        self, accounting_document: str, fiscal_year: str, company_code: str
    ) -> dict | None:
        items = self._query(
            _PAYMENT_SRV, _PAYMENT_SET, select=_PAYMENT_SELECT,
            filt=(
                f"AccountingDocument eq {_lit(accounting_document)} "
                f"and FiscalYear eq {_lit(fiscal_year)} "
                f"and CompanyCode eq {_lit(company_code)}"
            ),
            top=50,
        )
        if not items:
            return None
        cleared = any(_is_set(it.get("ClearingDate")) for it in items)
        return {
            "accountingDocument": accounting_document,
            "fiscalYear": fiscal_year,
            "companyCode": company_code,
            "isCleared": cleared,
            "clearingDate": next((it.get("ClearingDate") for it in items if _is_set(it.get("ClearingDate"))), None),
            "items": items,
        }

    def get_purchase_order_status(self, purchase_order: str) -> dict | None:
        return self._entity(_PO_SRV, _PO_SET, _lit(purchase_order), _PO_SELECT)

    def get_purchase_requisition_status(self, purchase_requisition: str) -> dict | None:
        header = self._entity(_PR_SRV, _PR_HEADER_SET, _lit(purchase_requisition), _PR_HEADER_SELECT)
        if header is None:
            return None
        header["items"] = self._query(
            _PR_SRV, _PR_ITEM_SET, select=_PR_ITEM_SELECT,
            filt=f"PurchaseRequisition eq {_lit(purchase_requisition)}", top=50,
        )
        return header

    def get_vendor_details(self, business_partner: str) -> dict | None:
        bp = self._entity(_BP_SRV, _BP_SET, _lit(business_partner), _BP_SELECT)
        if bp is None:
            return None
        # Company / purchasing block status is best-effort context, not required.
        try:
            supplier = self._entity(_BP_SRV, _SUPPLIER_SET, _lit(business_partner), _SUPPLIER_SELECT)
            if supplier:
                bp["supplierCompanyData"] = supplier
        except S4HANAError as exc:
            logger.info("supplier view for %s unavailable: %s", business_partner, exc)
        return bp

    # -- diagnostics ------------------------------------------------
    def ping(self) -> dict:
        """Cheap connectivity/auth check for ``/diag/s4`` — one row, one field."""
        body = self._get(f"/{_BP_SRV}/{_BP_SET}", {"$select": "BusinessPartner", "$top": "1"})
        return {"reachable": True, "rows": len(_rows(body))}


@functools.lru_cache(maxsize=1)
def get_s4hana_client() -> S4HANAClient:
    return S4HANAClient()
