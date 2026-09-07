"""Read-only S/4HANA access through the ``S43`` BTP destination.

Build step 3 (+ step 5 procure-to-pay widening). Read-only lookups over the
standard SAP S/4HANA Cloud OData v2 procure-to-pay APIs, every one a ``GET``:

===========================  =========================================  ============================
Lookup                       OData service                              Entity set
===========================  =========================================  ============================
invoice status / by vendor   ``API_SUPPLIERINVOICE_PROCESS_SRV``        ``A_SupplierInvoice``
invoice line items           ``API_SUPPLIERINVOICE_PROCESS_SRV``        ``A_SuplrInvcItemPurOrdRef``
payment clearing             ``API_OPLACCTGDOCITEMCUBE_SRV``            ``A_OperationalAcctgDocItemCube`` (falls back to ``API_JOURNALENTRYITEMBASIC_SRV``)
invoice -> payment status     ``…SUPPLIERINVOICE…`` + ``…JOURNALENTRY…``  (resolve FI doc, then clearing)
purchase order status        ``API_PURCHASEORDER_PROCESS_SRV``          ``A_PurchaseOrder``
purchase order items         ``API_PURCHASEORDER_PROCESS_SRV``          ``A_PurchaseOrderItem``
purchase order delivery date ``API_PURCHASEORDER_PROCESS_SRV``          ``A_PurchaseOrderScheduleLine``
purchase order approval      ``API_PURCHASEORDER_PROCESS_SRV``          ``A_PurchaseOrder`` (release fields)
purchase requisition status  ``API_PURCHASE_REQUISITION_SRV``            ``A_PurchaseRequisitionHeader`` / ``…Item``
goods receipts for a PO      ``API_MATERIAL_DOCUMENT_SRV``              ``A_MaterialDocumentItem``
three-way match (computed)   invoice + PO items + goods receipts        (composed from the rows above)
vendor / business partner    ``API_BUSINESS_PARTNER``                   ``A_BusinessPartner`` / ``A_Supplier``
vendor / contact email       ``API_BUSINESS_PARTNER``                   ``A_BusinessPartnerAddress`` -> ``A_AddressEmailAddress`` (PII exception — see get_vendor_email_addresses)
vendor bank account (IBAN)   ``API_BUSINESS_PARTNER``                   ``A_BusinessPartnerBank`` (PII/fraud-risk exception — see get_vendor_bank_accounts; NOT a bank statement, no live source for that)
budget status (computed)     Funds Mgmt / CO budget (none standard)     budgetAvailable=false always; actualSpend/commitmentValue computed from journal_entry_item / PO items
company code master          ``API_COMPANYCODE_SRV``                    ``A_CompanyCode``
cost centre master           ``API_COSTCENTER_SRV``                     ``A_CostCenter``
profit centre master         ``API_PROFITCENTER_SRV``                   ``A_ProfitCenter`` (+ computed ``isCurrentlyValid``)
G/L account master           ``API_GLACCOUNTINCHARTOFACCOUNTS_SRV``     ``A_GLAccountInChartOfAccounts``
G/L account activity         ``API_OPLACCTGDOCITEMCUBE_SRV`` (computed) same journal_entry_item source as get_cost_object_actuals, filtered by GLAccount
===========================  =========================================  ============================

The procure-to-pay APIs still mapped to the policy knowledge base only (no live
lookup yet, pending tenant activation): ``API_PURCHASE_ORDER_APPROVAL_SRV`` (the
dedicated workflow service — release state is read from the PO API for now),
``API_PAYMENT_DOCUMENT_SRV`` / ``API_BANK_STATEMENT_SRV``. The dedicated 3-way
match service ``API_PO_INVOICE_MATCH_SRV`` has no live lookup either — S/4HANA
does not expose its stored match result / block-release history via API, so
:meth:`S4HANAClient.check_three_way_match` computes the comparison from invoice
items, PO items and goods receipts and reads the resulting payment block off
the invoice header. See :mod:`ahf_finance_agent.domains`.

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
tenant-specific correction is a one-line change. Where a capability can be
served by more than one service (renamed / projection aliases, or a build that
omits one), ``_SERVICE_CATALOG`` lists the candidates and
:meth:`S4HANAClient.resolve_capability` probes the live tenant and locks onto
the first that answers; ``GET /diag/s4/catalog`` reports what resolved.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
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
    """The tenant's build does not expose a name we asked for. Two Gateway
    shapes mean this:

    * 404 ``Resource not found for the segment 'X'`` — a release-specific
      ``$select`` field, or a bad entity set.
    * 400 ``Property 'X' not found in type '….A_…Type'`` — same thing for a
      ``$select`` / ``$filter`` / ``$orderby`` field on a build whose CDS view
      omits it (seen on the S43 on-prem tenant for ``AccountingDocument`` on
      ``A_JournalEntryItemBasic``).

    Carries the offending name so :meth:`S4HANAClient._select_get` can drop it
    and, when it is a field we must filter on, the catalogue can fail over to
    the next candidate service.
    """

    def __init__(self, segment: str) -> None:
        super().__init__(segment)
        self.segment = segment


_BAD_SEGMENT_RE = re.compile(r"not found for the segment '([^']+)'", re.I)
# SAP Gateway's 400-flavoured way of saying the same thing:
#   "Property 'AccountingDocument' not found in type '…A_JournalEntryItemBasicType'"
_UNKNOWN_PROPERTY_RE = re.compile(r"[Pp]roperty '?([A-Za-z_]\w*)'? (?:was )?not found", re.I)


def _unknown_field(text: str) -> str | None:
    """The offending field/segment name if *text* is a Gateway 'you named a
    field this build does not have' error (the 404 'segment' form or the 400
    'Property … not found in type …' form), else ``None``."""
    m = _BAD_SEGMENT_RE.search(text) or _UNKNOWN_PROPERTY_RE.search(text)
    return m.group(1) if m else None


# --- service catalogue ----------------------------------------------------
# S/4HANA Cloud public OData v2 APIs. Adjust here if a tenant exposes a
# different service alias or custom projection.

_INVOICE_SRV = "API_SUPPLIERINVOICE_PROCESS_SRV"
_INVOICE_SET = "A_SupplierInvoice"
# NOTE: "IsPaid" is absent from the on-premise build of this service in the POC
# landscape and Gateway 404s the whole request on an unknown $select field
# ("Resource not found for the segment 'X'"). _select_get() self-heals by
# dropping any release-specific gap and retrying, at the cost of one wasted
# retry on that call. "AccountingDocument" is kept in the list on purpose: when
# the tenant exposes it, the answering loop can chain straight to a
# payment-clearing check without asking the user for the number; when it does
# not, _select_get() drops it.
_INVOICE_SELECT = (
    "SupplierInvoice", "FiscalYear", "CompanyCode", "DocumentDate", "PostingDate",
    "SupplierInvoiceStatus", "InvoicingParty", "InvoiceGrossAmount", "DocumentCurrency",
    "PaymentTerms", "DueCalculationBaseDate", "PaymentBlockingReason",
    "AccountingDocument", "AccountingDocumentType", "ReverseDocument",
)

_PAYMENT_SRV = "API_JOURNALENTRYITEMBASIC_SRV"
_PAYMENT_SET = "A_JournalEntryItemBasic"
_PAYMENT_SELECT = (
    "CompanyCode", "FiscalYear", "AccountingDocument", "AccountingDocumentItem",
    "AccountingDocumentType", "PostingDate", "DocumentDate",
    "AmountInCompanyCodeCurrency", "CompanyCodeCurrency", "DebitCreditCode",
    "GLAccount", "Supplier", "ClearingDate", "ClearingJournalEntry",
    "ClearingJournalEntryFiscalYear",
    # payment-side fields (F110 copies these onto the cleared / clearing lines).
    # Any the tenant's build does not expose are dropped by _select_get().
    "PaymentMethod", "PaymentMethodSupplement", "HouseBank", "HouseBankAccount",
    "PaymentReference", "PaymentDifferenceReason", "IsCleared",
)
# Fields on A_JournalEntryItemBasic that may carry the originating MM supplier
# invoice number (the FI "reference"/AWKEY). Tried in order to resolve a
# logistics invoice to its FI accounting document; a tenant that rejects one
# ($filter on a field its build does not expose) is skipped.
_JE_REFERENCE_FIELDS = ("OriginalReferenceDocument", "ReferenceDocument", "DocumentReferenceID")
# CO account-assignment fields on the journal_entry_item capability (the
# operational accounting-document cube), used to compute ACTUAL spend against
# a cost object when no budget/plan API is active — see get_budget_status().
# This is posted-actuals only, never a plan/budget figure.
_ACTUALS_SELECT = (
    "CompanyCode", "FiscalYear", "FiscalPeriod", "PostingDate",
    "AmountInCompanyCodeCurrency", "CompanyCodeCurrency", "DebitCreditCode",
    "GLAccount", "CostCenter", "OrderID", "WBSElement",
    "PurchaseOrder", "PurchaseOrderItem",
)
# Same journal_entry_item cube, selected for a company-wide AP/AR OPEN ITEMS
# summary (get_accounts_payable_summary / get_accounts_receivable_summary)
# instead of one cost object or G/L account. "Supplier" is CONFIRMED present
# and populated on this tenant (it's already relied on in production by
# get_payment_clearing_status / the FI-doc reference resolver). "Customer" is
# ALSO NOW CONFIRMED (2026-09, post-deploy live check against company code
# 1710 via get_accounts_receivable_summary — it returned a real open item, not
# an empty/dropped result) — this cube's underlying CDS view (the S/4HANA
# universal journal) does carry it alongside Supplier on this tenant. The
# runtime detection in get_accounts_receivable_summary (every row simply
# lacking a "Customer" key => report not-connected instead of a fabricated
# zero) is kept anyway as cheap insurance for a different tenant/build where
# it might not hold — do not remove it on the strength of this one check.
_OPEN_ITEMS_SELECT = (
    "CompanyCode", "FiscalYear", "AccountingDocument", "AccountingDocumentItem",
    "PostingDate", "DocumentDate", "AmountInCompanyCodeCurrency", "CompanyCodeCurrency",
    "DebitCreditCode", "Supplier", "Customer", "ClearingDate",
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
# PO line items — ordered quantity and net price, needed to compute an
# invoice-vs-PO-vs-GR (three-way) variance. Same OData service as the PO header.
_PO_ITEM_SET = "A_PurchaseOrderItem"
_PO_ITEM_SELECT = (
    "PurchaseOrder", "PurchaseOrderItem", "PurchaseOrderItemText", "Material",
    "Plant", "OrderQuantity", "PurchaseOrderQuantityUnit", "NetPriceAmount",
    "NetPriceQuantity", "OrderPriceUnit", "DocumentCurrency", "NetAmount",
    "IsCompletelyDelivered", "IsFinallyInvoiced", "InvoiceIsGoodsReceiptBased",
    "PurchasingDocumentDeletionCode",
)
# PO delivery schedule lines — where the delivery date lives (the PO header and
# item carry none). Same OData service; the sub-node is not activated on every
# on-premise build, so callers must tolerate an S4HANAError here.
_PO_SCHEDULE_SET = "A_PurchaseOrderScheduleLine"
_PO_SCHEDULE_SELECT = (
    "PurchaseOrder", "PurchaseOrderItem", "ScheduleLine", "ScheduleLineDeliveryDate",
    "ScheduleLineDeliveryTime", "ScheduleLineOrderQuantity", "PurchaseOrderQuantityUnit",
    "PurgReqnDelivDate",
)

# The "Purchase Requisition" API (Manage Purchase Requisitions Fiori app),
# not the leaner "process" projection ``API_PURCHASEREQ_PROCESS_SRV`` — the
# process one omits header fields such as ``CreatedByUser`` in some tenant
# builds, which _select_get() would then silently drop from the projection.
# Same entity-set names, so this is a one-line repoint.
_PR_SRV = "API_PURCHASE_REQUISITION_SRV"
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

# Purchase-order release / approval state. Served by the PO process API today
# (the ``PurchaseOrderIsReleased`` / ``ReleaseIsNotCompleted`` fields on
# A_PurchaseOrder). A tenant that exposes the dedicated approval-workflow
# service can repoint this constant at ``API_PURCHASE_ORDER_APPROVAL_SRV``.
_PO_APPROVAL_SRV = _PO_SRV
_PO_APPROVAL_SET = _PO_SET
_PO_APPROVAL_SELECT = (
    "PurchaseOrder", "PurchaseOrderType", "CompanyCode", "Supplier",
    "PurchaseOrderDate", "CreationDate", "PurchaseOrderIsReleased",
    "ReleaseIsNotCompleted", "PurchasingCompletenessStatus",
    "PurchasingProcessingStatus", "PurchasingDocumentDeletionCode",
)

# Supplier-invoice line items (purchase-order referenced). Same OData service as
# the invoice header; released alias ``API_SUPPLIER_INVOICE_ITEM_SRV``.
# The OData entity set is the ABBREVIATED name ``A_SuplrInvcItemPurOrdRef``
# (nav property ``to_SuplrInvcItemPurOrdRef``) — the un-abbreviated CDS view
# name ``A_SupplierInvoiceItemPurOrdReference`` is not an exposed segment and
# Gateway 404s the whole request ("no resource/segment").
_INVOICE_ITEM_SET = "A_SuplrInvcItemPurOrdRef"
_INVOICE_ITEM_SELECT = (
    "SupplierInvoice", "FiscalYear", "SupplierInvoiceItem", "PurchaseOrder",
    "PurchaseOrderItem", "Plant", "DocumentCurrency", "SupplierInvoiceItemAmount",
    "PurchaseOrderQuantityUnit", "QuantityInPurchaseOrderUnit",
    "SupplierInvoiceQuantityUnit", "QuantityInSupplierInvoiceUnit",
    "TaxCode", "IsSubsequentDebitCredit", "SupplierInvoiceItemText",
)

# Goods receipts / inbound material documents booked against a purchase order.
# Standard released API; the process-tier alias is ``API_MATERIAL_DOCUMENT_SRV``
# (``API_GOODS_RECEIPT_SRV`` is the projection view over the same data).
_MATERIAL_DOC_SRV = "API_MATERIAL_DOCUMENT_SRV"
_MATERIAL_DOC_ITEM_SET = "A_MaterialDocumentItem"
_MATERIAL_DOC_ITEM_SELECT = (
    "MaterialDocument", "MaterialDocumentYear", "MaterialDocumentItem",
    "Material", "Plant", "StorageLocation", "GoodsMovementType",
    "PurchaseOrder", "PurchaseOrderItem", "QuantityInEntryUnit", "EntryUnit",
    "QuantityInBaseUnit", "MaterialBaseUnit", "GoodsMovementRefDocType",
    "DebitCreditCode", "InventoryUsabilityCode", "GoodsMovementIsCancelled",
    "ReferenceDocument", "PostingDate", "DocumentDate", "CreationDate",
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
# BP address -> email address. Same service; used only by
# get_vendor_email_addresses(), which is a DELIBERATE, narrow exception to the
# EmailAddress PII strip (see that method's docstring).
_BP_ADDRESS_SET = "A_BusinessPartnerAddress"
_BP_ADDRESS_SELECT = ("BusinessPartner", "AddressID")
_EMAIL_SET = "A_AddressEmailAddress"
_EMAIL_SELECT = (
    "AddressID", "Person", "OrdinalNumber", "EmailAddress",
    "IsDefaultEmailAddress", "SearchEmailAddress",
)
# BP bank master data (routing details for a vendor payment — IBAN, bank key,
# account holder). Same service; used only by get_vendor_bank_accounts(),
# a SECOND deliberate, narrow exception to the bank-data strip (see that
# method's docstring). This is NOT a bank statement (TR-CM transaction data) —
# there is no live source for that on this tenant.
_BP_BANK_SET = "A_BusinessPartnerBank"
_BP_BANK_SELECT = (
    "BusinessPartner", "BankIdentification", "BankCountryKey", "BankName", "BankNumber",
    "BankAccount", "BankAccountName", "IBAN", "IBANValidityStartDate", "SWIFTCode",
    "BankAccountHolderName", "BankControlKey", "CityName",
    "ValidityStartDate", "ValidityEndDate",
)

_CC_SRV = "API_COMPANYCODE_SRV"
_CC_SET = "A_CompanyCode"
_CC_SELECT = (
    "CompanyCode", "CompanyCodeName", "CityName", "Country", "Currency",
    "Language", "ChartOfAccounts", "FiscalYearVariant",
)

_COST_CENTER_SRV = "API_COSTCENTER_SRV"
_COST_CENTER_SET = "A_CostCenter"
# Master data (validity, ownership, currency) — distinct from
# get_cost_object_actuals(), which sums live *postings* against the cost
# centre. "PersonResponsible" is a guess (unverified) and dropped by self-heal
# if wrong — see the A_ProfitCenter lesson below: this tenant names that field
# differently per entity (A_ProfitCenter uses ProfitCtrResponsiblePersonName /
# ProfitCtrResponsibleUser, not a plain "PersonResponsible"). The description
# text (if any) lives behind a to_Text navigation, not inline, so it is not
# requested here.
_COST_CENTER_SELECT = (
    "CostCenter", "ControllingArea", "ValidityStartDate", "ValidityEndDate",
    "CompanyCode", "PersonResponsible", "CostCenterCategory", "ProfitCenter",
    "Currency",
)

_PROFIT_CENTER_SRV = "API_PROFITCENTER_SRV"
_PROFIT_CENTER_SET = "A_ProfitCenter"
# Confirmed against this tenant's live data (2026-09) — the earlier guessed
# fields (ProfitCenterName / Description / Name / SegmentName) do not exist at
# all; the real block/responsibility/segment fields are named quite
# differently, and there is no inline description (it's behind to_Text, not
# requested here — nothing in this agent needs it beyond what's below).
_PROFIT_CENTER_SELECT = (
    "ProfitCenter", "ControllingArea", "ValidityStartDate", "ValidityEndDate",
    "ProfitCenterIsBlocked", "ProfitCtrResponsiblePersonName",
    "ProfitCtrResponsibleUser", "Segment", "ProfitCenterStandardHierarchy",
    "CompanyCode",
)

_GL_MASTER_SRV = "API_GLACCOUNTINCHARTOFACCOUNTS_SRV"
_GL_MASTER_SET = "A_GLAccountInChartOfAccounts"
_GL_MASTER_SELECT = (
    "ChartOfAccounts", "GLAccount", "GLAccountGroup", "IsBalanceSheetAccount",
    "AccountIsBlockedForPosting", "AccountIsBlockedForPlanning",
    "ShortText", "GLAccountLongText",
)

# This tenant runs most OData services against the destination's default SAP
# client, but a few are only configured/activated in a different one —
# confirmed by hitting a service's $metadata directly with ?sap-client=NNN.
# Requesting the wrong client for one of these looks exactly like a login
# failure (401), not a clean "service not found", so misdiagnosing this as a
# broader destination-auth outage is an easy mistake (happened once already,
# for PO). Add a service name here (as it appears in the URL) only once
# confirmed against the tenant; every service without an entry keeps using the
# destination's default (400) unchanged.
_SAP_CLIENT_OVERRIDE: dict[str, str] = {
    _PO_SRV: "100",  # API_PURCHASEORDER_PROCESS_SRV — confirmed 2026-09
    # Tried speculatively (2026-09) for goods-receipt lookups, which showed the
    # same 401-shaped symptom as PO before the fix above. Unconfirmed — if GR
    # lookups still fail (or now return empty/wrong data) after this, it was
    # the wrong guess; remove these two lines rather than assume they're right.
    _MATERIAL_DOC_SRV: "100",
    "API_GOODS_RECEIPT_SRV": "100",
    # Confirmed 2026-09: same client-100 pattern for the CO/FI master-data
    # services added alongside the PO/GR ones — user hit $metadata directly
    # with ?sap-client=100 vs 400 and confirmed only 100 returns data.
    _CC_SRV: "100",
    _COST_CENTER_SRV: "100",
    _PROFIT_CENTER_SRV: "100",
    _GL_MASTER_SRV: "100",
}


# --- capability catalogue --------------------------------------------------
# Each logical capability maps to an ordered list of ``(service, entity_set)``
# candidates. :meth:`S4HANAClient.resolve_capability` probes them against the
# live tenant and locks onto the first that answers, so a service that a given
# S/4HANA build names differently — or does not expose at all — self-corrects
# to the next candidate instead of failing the lookup outright.
# :meth:`S4HANAClient.probe_catalog` (and ``GET /diag/s4/catalog``) report what
# resolved per capability, making "is this connected on this tenant" checkable.
_SERVICE_CATALOG: dict[str, tuple[tuple[str, str], ...]] = {
    "supplier_invoice_header": (
        (_INVOICE_SRV, _INVOICE_SET),
    ),
    "supplier_invoice_item": (
        (_INVOICE_SRV, _INVOICE_ITEM_SET),
        (_INVOICE_SRV, "A_SupplierInvoiceItemPurOrdReference"),
        ("API_SUPPLIER_INVOICE_ITEM_SRV", "A_SupplierInvoiceItemPurOrdReference"),
    ),
    "journal_entry_item": (
        # API_OPLACCTGDOCITEMCUBE_SRV tried first: on this tenant's build,
        # API_JOURNALENTRYITEMBASIC_SRV's A_JournalEntryItemBasic is missing
        # AccountingDocument (see _UnknownODataSegment / _UNKNOWN_PROPERTY_RE
        # above), which breaks payment-clearing lookups. The operational
        # accounting-document cube carries the same standard field names
        # (AccountingDocument, ClearingDate, ...) and is the confirmed-working
        # journal/accounting-document source here.
        ("API_OPLACCTGDOCITEMCUBE_SRV", "A_OperationalAcctgDocItemCube"),
        (_PAYMENT_SRV, _PAYMENT_SET),
        ("API_JOURNAL_ENTRY_SRV", "A_JournalEntryItem"),
    ),
    "purchase_order_header": (
        (_PO_SRV, _PO_SET),
    ),
    "purchase_order_item": (
        (_PO_SRV, _PO_ITEM_SET),
    ),
    "purchase_order_schedule_line": (
        (_PO_SRV, _PO_SCHEDULE_SET),
    ),
    "goods_receipt_item": (
        (_MATERIAL_DOC_SRV, _MATERIAL_DOC_ITEM_SET),
        ("API_GOODS_RECEIPT_SRV", _MATERIAL_DOC_ITEM_SET),
        ("API_INBOUND_DELIVERY_SRV", "A_InbDeliveryItem"),
    ),
    "purchase_requisition_header": (
        (_PR_SRV, _PR_HEADER_SET),
        ("API_PURCHASEREQ_PROCESS_SRV", _PR_HEADER_SET),
    ),
    "purchase_requisition_item": (
        (_PR_SRV, _PR_ITEM_SET),
        ("API_PURCHASEREQ_PROCESS_SRV", _PR_ITEM_SET),
    ),
    "business_partner": (
        (_BP_SRV, _BP_SET),
    ),
    "supplier": (
        (_BP_SRV, _SUPPLIER_SET),
    ),
    "business_partner_address": (
        (_BP_SRV, _BP_ADDRESS_SET),
    ),
    "address_email": (
        (_BP_SRV, _EMAIL_SET),
    ),
    "business_partner_bank": (
        (_BP_SRV, _BP_BANK_SET),
    ),
    "company_code": (
        (_CC_SRV, _CC_SET),
    ),
    "cost_center": (
        (_COST_CENTER_SRV, _COST_CENTER_SET),
    ),
    "profit_center": (
        (_PROFIT_CENTER_SRV, _PROFIT_CENTER_SET),
    ),
    "gl_account_master": (
        (_GL_MASTER_SRV, _GL_MASTER_SET),
    ),
    # Budget / availability control. No released OData surface is reliably
    # present across S/4HANA builds (Funds Management, CO planning and internal-
    # order budgeting are each optional and mostly BAPI-only), so these are
    # best-effort probes: when none resolves, get_budget_status returns a typed
    # "not connected" pointing at the right report.
    "budget": (
        ("API_BUDGET_ENTRY_DOCUMENT_SRV", "A_BudgetEntryDocument"),
        ("API_FUNDSMGMTBUDGET_SRV", "A_FundsManagementBudget"),
        ("API_CONTROLLING_BUDGET_SRV", "A_ControllingBudget"),
    ),
}

_CATALOG_TTL_SECONDS = 3600.0
# A 401 means the destination's credentials are rejected system-wide — every
# service and every catalogue candidate will 401 identically. Retrying that
# immediately on every subsequent call (e.g. 11 capabilities x up to 3
# candidates each on one /diag/s4/catalog probe, or the ~6 chained calls one
# check_three_way_match turn makes) just spams S/4HANA with more failed
# logons, which on a tenant with a lockout policy can turn "wrong password"
# into "now also locked". Short-circuit for this long instead.
#
# Kept short deliberately: S4HANAClient is the process-wide singleton
# (get_s4hana_client()), so this cooldown is shared by every request the
# server handles, not just the one that tripped it. Its job is only to
# collapse ONE burst of chained/probing calls into a single failed logon, not
# to sit in front of unrelated later questions (including from other
# sessions) — those should get a fresh attempt against S/4HANA, since a 401
# here has repeatedly turned out to be intermittent rather than permanently
# broken credentials.
_AUTH_COOLDOWN_SECONDS = 8.0


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


def _strip_odata_plumbing(row: dict) -> dict:
    """Drop OData ``__metadata`` / ``__deferred`` nav placeholders only — no PII
    strip. Used only by :meth:`S4HANAClient.get_vendor_email_addresses`, which
    deliberately does not run :func:`strip_sensitive_keys` (see its docstring).
    Every other row in this module goes through :func:`_clean` instead."""
    out = {}
    for key, value in row.items():
        if key == "__metadata":
            continue
        if isinstance(value, dict) and "__deferred" in value:
            continue
        out[key] = value
    return out


def _clean(row: dict) -> dict:
    """Drop OData plumbing and any sensitive key that slipped into a projection."""
    return strip_sensitive_keys(_strip_odata_plumbing(row))


def _is_set(value: Any) -> bool:
    """True when an OData date/id field carries a real value (not empty / epoch)."""
    if value in (None, "", 0):
        return False
    text = str(value)
    return text not in ("0", "/Date(0)/", "0000-00-00", "0000-00-00T00:00:00")


def _truthy(value: Any) -> bool:
    """Interpret an OData boolean-ish value (``True`` / ``"true"`` / ``"X"``)."""
    return str(value).strip().lower() in ("true", "x", "1", "yes")


def _num(value: Any) -> float | None:
    """Parse an OData decimal (usually a string like ``"212652.32"``) to float."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _norm_item(value: Any) -> str:
    """Normalise a document item number so ``"00010"`` and ``"10"`` compare equal."""
    return (str(value or "").strip().lstrip("0")) or "0"


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


_SAP_DATE_MS_RE = re.compile(r"/Date\((-?\d+)")


def _sap_date_ms(value: Any) -> int | None:
    """Milliseconds-since-epoch out of an OData ``/Date(1719360000000+0000)/``
    string, or ``None`` if *value* isn't one."""
    if not isinstance(value, str):
        return None
    m = _SAP_DATE_MS_RE.search(value)
    return int(m.group(1)) if m else None


def _is_currently_valid(start: Any, end: Any) -> bool | None:
    """Whether *now* falls within [*start*, *end*] (both OData ``/Date(...)/``
    values, either optional). ``None`` when neither carries a usable date —
    "can't tell", not "no"."""
    start_ms, end_ms = _sap_date_ms(start), _sap_date_ms(end)
    if start_ms is None and end_ms is None:
        return None
    now_ms = time.time() * 1000
    if start_ms is not None and now_ms < start_ms:
        return False
    if end_ms is not None and now_ms > end_ms:
        return False
    return True


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
    """Read-only OData client for the in-scope S/4HANA procure-to-pay services.

    Stateless between calls: each request re-resolves the ``S43`` destination
    (cheap — :func:`resolve_destination` caches the token until it nears
    expiry) and uses a fresh short-lived ``httpx.Client``, so a rotated token
    is never a problem.

    Not stateless about failure, though: a 401 on a given OData service arms a
    ``_AUTH_COOLDOWN_SECONDS`` cooldown for THAT SERVICE, so subsequent calls
    to it short-circuit with a lighter "retry shortly" error instead of
    hitting S/4HANA again — repeatedly retrying a known-bad service (e.g.
    across every capability-catalogue candidate on one ``/diag/s4/catalog``
    call) just spams the tenant with failed logons, which under a lockout
    policy can turn a bad password into a locked account. Scoped per service
    rather than the whole destination on purpose: on this tenant a 401 has
    repeatedly turned out to be one service needing a different SAP client
    (see ``_SAP_CLIENT_OVERRIDE``), not the destination's credentials being
    globally wrong — a global cooldown would pause every other, working
    service too every time one misconfigured service 401s.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        # capability -> (service, entity_set, expires_at); per instance so the
        # process-wide singleton (get_s4hana_client) keeps its resolution warm
        # while a fresh client in a test starts clean.
        self._cap_cache: dict[str, tuple[str, str, float]] = {}
        self._cap_lock = threading.Lock()
        # service name -> monotonic() deadline until which _get() short-circuits
        # calls to THAT service with a "retry shortly" error instead of hitting
        # S/4HANA again. Scoped per service, not the whole client: see
        # _AUTH_COOLDOWN_SECONDS and the comment in _get().
        self._auth_broken_until: dict[str, float] = {}

    # -- capability catalogue --------------------------------------------
    def _probe(self, service: str, entity_set: str) -> str:
        """Classify a candidate ``(service, entity_set)`` against the tenant:

        * ``"ok"``          — it answered (or refused our bare ``$top=1`` with a
                              400, which still proves the service is usable).
        * ``"absent"``      — 403 (not authorised / not in this tenant's
                              communication arrangement), 404 / unknown segment
                              (not activated), or 501. Try the next candidate.
        * ``"unreachable"`` — destination / transport failure, 401 (whole-system
                              auth), or 5xx. Tells us nothing; do not lock.
        """
        return self._probe_ex(service, entity_set)[0]

    def _probe_ex(self, service: str, entity_set: str) -> tuple[str, str | None]:
        """:meth:`_probe` plus a short human detail (the error) for diagnostics."""
        try:
            self._get(f"/{service}/{entity_set}", {"$top": "1"})
            return "ok", None
        except _UnknownODataSegment as exc:
            return "absent", f"unknown segment {exc.segment!r}"
        except S4HANAError as exc:
            m = str(exc)
            ml = m.lower()
            if any(w in ml for w in ("cannot resolve", "request to", "non-json", "connect", "timed out", "timeout")):
                return "unreachable", m[:200]
            code_match = re.search(r"returned (\d{3})", ml)
            code = int(code_match.group(1)) if code_match else None
            if code == 400:
                return "ok", None
            if code in (403, 404, 501) or "no odata error body" in ml:
                return "absent", m[:200]
            return "unreachable", m[:200]

    def resolve_capability(
        self, capability: str, *, skip: frozenset[tuple[str, str]] = frozenset(), refresh: bool = False
    ) -> tuple[str, str]:
        """Return the ``(service, entity_set)`` to use for *capability*, probing
        the catalogue candidates once and caching the winner. ``skip`` excludes
        candidates already known bad (used to advance past a renamed entity
        set at query time)."""
        candidates = _SERVICE_CATALOG.get(capability)
        if not candidates:
            raise S4HANAError(f"unknown S/4HANA capability {capability!r}")
        if not skip and not refresh:
            with self._cap_lock:
                hit = self._cap_cache.get(capability)
                if hit and hit[2] > time.monotonic():
                    return hit[0], hit[1]

        usable = [c for c in candidates if c not in skip]
        chosen = next((c for c in usable if self._probe(*c) == "ok"), None)
        result = chosen or (usable[0] if usable else candidates[0])
        if chosen is not None and not skip:
            with self._cap_lock:
                self._cap_cache[capability] = (result[0], result[1], time.monotonic() + _CATALOG_TTL_SECONDS)
        return result

    def probe_catalog(self) -> dict:
        """Probe every catalogue capability against the live tenant. Used by
        ``GET /diag/s4/catalog`` and the startup warm-up."""
        out: dict = {}
        for capability, candidates in _SERVICE_CATALOG.items():
            tried: list[dict] = []
            resolved: str | None = None
            for service, entity_set in candidates:
                status, detail = self._probe_ex(service, entity_set)
                tried.append({"service": f"{service}/{entity_set}", "status": status, "detail": detail})
                if status == "ok" and resolved is None:
                    resolved = f"{service}/{entity_set}"
            if resolved:
                srv, es = resolved.split("/", 1)
                with self._cap_lock:
                    self._cap_cache[capability] = (srv, es, time.monotonic() + _CATALOG_TTL_SECONDS)
            out[capability] = {
                "resolved": resolved,
                "ok": resolved is not None,
                "candidates": tried,
            }
        return out

    @staticmethod
    def _should_try_next_candidate(exc: S4HANAError, entity_set: str) -> bool:
        """True when an error on the resolved candidate means 'this service is
        not usable here' (renamed entity set, a field its build does not expose
        that we must ``$filter`` on, 403 not-authorised, 404 not activated, 501)
        rather than a genuine data / query problem that the next candidate would
        hit too (400) or a transport failure."""
        m = str(exc)
        if f"resource/segment '{entity_set}'" in m:
            return True
        # _select_get() exhausted its retries: a name we need (often a $filter
        # key such as AccountingDocument) is not on this candidate's entity
        # type. The next catalogue candidate may model it — e.g. the S43 tenant
        # serves journal-entry items from API_OPLACCTGDOCITEMCUBE_SRV, not
        # API_JOURNALENTRYITEMBASIC_SRV.
        if "has no resource/segment" in m:
            return True
        return bool(re.search(r"returned (403|404|501)\b", m))

    def _remaining_candidates(self, capability: str, skip: set[tuple[str, str]]) -> int:
        return sum(1 for c in _SERVICE_CATALOG.get(capability, ()) if c not in skip)

    def _advance(self, capability: str, srv: str, es: str, skip: set[tuple[str, str]]) -> None:
        skip.add((srv, es))
        with self._cap_lock:
            self._cap_cache.pop(capability, None)
        logger.warning("capability %r: %s/%s not usable here, trying next candidate", capability, srv, es)

    def _capability_query(self, capability: str, *, select, filt, **kw) -> list[dict]:
        """:meth:`_query` routed through the catalogue, advancing to the next
        candidate if the resolved service turns out not to be usable here."""
        skip: set[tuple[str, str]] = set()
        while True:
            srv, es = self.resolve_capability(capability, skip=frozenset(skip))
            try:
                return self._query(srv, es, select=select, filt=filt, **kw)
            except S4HANAError as exc:
                if self._should_try_next_candidate(exc, es) and self._remaining_candidates(capability, skip) > 1:
                    self._advance(capability, srv, es, skip)
                    continue
                raise

    def _capability_entity(self, capability: str, key_predicate: str, select) -> dict | None:
        skip: set[tuple[str, str]] = set()
        while True:
            srv, es = self.resolve_capability(capability, skip=frozenset(skip))
            try:
                return self._entity(srv, es, key_predicate, select)
            except S4HANAError as exc:
                if self._should_try_next_candidate(exc, es) and self._remaining_candidates(capability, skip) > 1:
                    self._advance(capability, srv, es, skip)
                    continue
                raise

    # -- transport ----------------------------------------------------
    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        s = self._s
        # Extracted up front (not just for the sap-client override below) so
        # the auth cooldown can be scoped per SERVICE rather than the whole
        # destination — a 401 on this tenant has repeatedly turned out to be
        # one service needing a different SAP client (see
        # _SAP_CLIENT_OVERRIDE), not the destination's credentials being
        # rejected outright. A global cooldown would pause every OTHER
        # (working) service too every time one misconfigured service 401s.
        service_name = path.strip("/").split("/", 1)[0]
        remaining = self._auth_broken_until.get(service_name, 0.0) - time.monotonic()
        if remaining > 0:
            # Deliberately a distinct, lighter message from the original 401
            # (below): this is a brief, likely-transient cooldown after a
            # recent failed logon elsewhere, not a fresh diagnostic — phrase it
            # as "retry shortly", not as a fresh systemic failure.
            raise S4HANAError(
                f"S/4HANA lookup on {service_name} temporarily paused ({remaining:.0f}s left): a "
                f"recent call to this service hit a logon failure, so further calls to it are "
                "briefly held off to avoid piling on more failed logons. This is usually transient "
                "— retry in a few seconds. Other services are unaffected."
            )
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
        client_override = _SAP_CLIENT_OVERRIDE.get(service_name)
        if client_override:
            query["sap-client"] = client_override
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
                seg = _unknown_field(resp.text)
                if seg:
                    raise _UnknownODataSegment(seg)
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
        if resp.status_code == 401:
            logger.error("S/4HANA 401 on %s (service=%s) — %r destination credentials rejected (body=%s)",
                         url, service_name, s.s4hana_destination_name, resp.text[:300])
            self._auth_broken_until[service_name] = time.monotonic() + _AUTH_COOLDOWN_SECONDS
            raise S4HANAError(
                f"S/4HANA returned 401 for {url!r}: the {s.s4hana_destination_name!r} destination's "
                f"credentials were rejected (logon failed) for {service_name!r}. On this tenant that "
                "has repeatedly turned out to be THIS SPECIFIC SERVICE needing a different SAP client "
                f"(see _SAP_CLIENT_OVERRIDE — confirm via {service_name}/$metadata?sap-client=NNN), "
                "rather than the destination's credentials being globally wrong — check that first. "
                "Not a missing record. Further calls to this service are being briefly held off "
                f"({_AUTH_COOLDOWN_SECONDS:.0f}s) to avoid piling on more failed logons (a lockout "
                "policy can turn a bad password into a locked account); other services are unaffected."
            )
        if resp.status_code == 403:
            logger.error("S/4HANA 403 on %s — %r destination user not authorised for this service (body=%s)",
                         url, s.s4hana_destination_name, resp.text[:300])
            raise S4HANAError(
                f"S/4HANA returned 403 for {url!r}: the {s.s4hana_destination_name!r} destination's "
                "user authenticated but is not authorised for this OData service / entity set "
                "(activate it in /IWFND/MAINT_SERVICE and grant S_SERVICE in the user's role, or "
                "add it to the communication arrangement). Not a missing record."
            )
        if resp.is_error:
            # A 400 can also be "you named a field this build does not have"
            # ("Property 'X' not found in type '…'"). Treat it like the 404
            # 'segment' form so _select_get() can drop the field / the catalogue
            # can fail over, instead of dead-ending the whole lookup.
            if _is_odata_error_body(resp):
                seg = _unknown_field(resp.text)
                if seg:
                    raise _UnknownODataSegment(seg)
            logger.error("S/4HANA error: url=%s status=%s body=%s", url, resp.status_code, resp.text[:300])
            raise S4HANAError(_odata_error(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise S4HANAError(f"S/4HANA returned non-JSON ({resp.status_code}) for {url!r}") from exc

    def _select_get(self, path: str, select: tuple[str, ...], params: dict[str, str]) -> Any:
        """``_get`` with a self-healing ``$select``: if the service rejects a
        field ("Resource not found for the segment 'X'", or the 400-flavoured
        "Property 'X' not found in type '…'"), drop it and retry, so a
        release-specific field gap degrades to a smaller projection instead of
        failing the whole lookup. A rejected name that is not in ``$select``
        (the entity set itself, or a ``$filter`` key the build does not have)
        is re-raised as an ``S4HANAError`` — the catalogue turns that into a
        fail-over to the next candidate service where one exists."""
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
        self, srv: str, entity_set: str, *, select: tuple[str, ...], filt: str,
        top: int = 20, orderby: str | None = None, skip: int = 0,
    ) -> list[dict]:
        params = {"$filter": filt, "$top": str(max(1, min(top, 50)))}
        if skip and skip > 0:
            params["$skip"] = str(skip)
        if orderby:
            params["$orderby"] = orderby
        return [_clean(r) for r in _rows(self._select_get(f"/{srv}/{entity_set}", select, params))]

    # -- lookups ----------------------------------------------------
    def get_invoice_status(self, invoice: str, fiscal_year: str | None = None) -> dict | None:
        """Look up a supplier invoice by its number. The number is unique on its
        own, so ``fiscal_year`` is optional — pass it only to disambiguate the
        rare case of a reused number across years.

        Uses ``$filter`` rather than a read-by-key
        ``A_SupplierInvoice(SupplierInvoice='..',FiscalYear='..')``: several
        on-premise Gateway builds 404 the composite-key GET while the collection
        query returns the row fine, and the key form needs a fiscal year anyway.
        """
        filt = f"SupplierInvoice eq {_lit(invoice)}"
        if fiscal_year:
            filt += f" and FiscalYear eq {_lit(fiscal_year)}"
        rows = self._query(
            _INVOICE_SRV, _INVOICE_SET, select=_INVOICE_SELECT,
            filt=filt, top=5, orderby="PostingDate desc",
        )
        if len(rows) > 1:
            logger.warning(
                "invoice %s matched %d rows across fiscal years; returning the most recent",
                invoice, len(rows),
            )
        return rows[0] if rows else None

    def search_invoices_by_vendor(
        self, invoicing_party: str, company_code: str | None = None, *, top: int = 10, skip: int = 0
    ) -> list[dict]:
        filt = f"InvoicingParty eq {_lit(invoicing_party)}"
        if company_code:
            filt += f" and CompanyCode eq {_lit(company_code)}"
        return self._query(
            _INVOICE_SRV, _INVOICE_SET, select=_INVOICE_SELECT, filt=filt,
            top=top, skip=skip, orderby="PostingDate desc",
        )

    def count_invoices_by_vendor(self, invoicing_party: str) -> int:
        body = self._get(
            f"/{_INVOICE_SRV}/{_INVOICE_SET}/$count",
            {"$filter": f"InvoicingParty eq {_lit(invoicing_party)}"},
        )
        try:
            return int(body) if isinstance(body, (int, str)) else len(_rows(body))
        except (TypeError, ValueError):
            return len(_rows(body))

    def get_payment_clearing_status(
        self, accounting_document: str, fiscal_year: str, company_code: str
    ) -> dict | None:
        items = self._capability_query(
            "journal_entry_item", select=_PAYMENT_SELECT,
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
        clearing_date = next((it.get("ClearingDate") for it in items if _is_set(it.get("ClearingDate"))), None)
        result = {
            "accountingDocument": accounting_document,
            "fiscalYear": fiscal_year,
            "companyCode": company_code,
            "isCleared": cleared,
            "clearingDate": clearing_date,
            "items": items,
            "paymentSummary": self._payment_summary(items, company_code) if cleared else None,
        }
        return result

    def _payment_summary(self, invoice_items: list[dict], company_code: str) -> dict:
        """Describe the payment that cleared an invoice: the payment document,
        date, method, house bank and amount — everything F110 records except the
        run ID (LAUFD/LAUFI), which no released API exposes. Built from the
        cleared invoice lines plus, best-effort, the clearing document's own
        bank line."""

        def _first(key: str, rows: list[dict]) -> Any:
            return next((r.get(key) for r in rows if _is_set(r.get(key))), None)

        pay_doc = _first("ClearingJournalEntry", invoice_items)
        pay_fy = _first("ClearingJournalEntryFiscalYear", invoice_items) or _first("FiscalYear", invoice_items)

        clearing_lines: list[dict] = []
        if pay_doc and pay_fy and company_code:
            try:
                clearing_lines = self._capability_query(
                    "journal_entry_item", select=_PAYMENT_SELECT,
                    filt=(
                        f"AccountingDocument eq {_lit(str(pay_doc))} "
                        f"and FiscalYear eq {_lit(str(pay_fy))} "
                        f"and CompanyCode eq {_lit(company_code)}"
                    ),
                    top=50,
                )
            except S4HANAError as exc:
                logger.info("clearing document %s lines unavailable: %s", pay_doc, exc)

        bank_line = next((ln for ln in clearing_lines if _is_set(ln.get("HouseBank"))), None) or {}
        return {
            "paymentDocument": pay_doc,
            "paymentDocumentFiscalYear": pay_fy,
            "clearingDate": _first("ClearingDate", invoice_items),
            "paymentMethod": _first("PaymentMethod", clearing_lines) or _first("PaymentMethod", invoice_items),
            "houseBank": bank_line.get("HouseBank"),
            "houseBankAccount": bank_line.get("HouseBankAccount"),
            "paymentReference": _first("PaymentReference", clearing_lines) or _first("PaymentReference", invoice_items),
            "paymentAmount": bank_line.get("AmountInCompanyCodeCurrency"),
            "paymentCurrency": bank_line.get("CompanyCodeCurrency") or _first("CompanyCodeCurrency", invoice_items),
            "runIdNote": (
                "The F110 payment-run ID (LAUFD/LAUFI) is not exposed by any released S/4HANA "
                "API. The payment document + date + house bank above identify the run for AP "
                "Payments (FBL1N / F110 history / payment medium)."
            ),
        }

    def _find_accounting_document_by_reference(
        self, invoice: str, fiscal_year: str, company_code: str
    ) -> str | None:
        """Resolve an MM supplier invoice to its FI accounting document by
        matching it against the journal-entry 'reference document' (AWKEY).

        Tries each candidate reference field / key shape; a field the tenant's
        build does not expose ($filter rejected) is skipped. Returns ``None``
        if nothing matches or we lack the company code / fiscal year to scope
        the query."""
        if not (fiscal_year and company_code):
            return None
        for field in _JE_REFERENCE_FIELDS:
            for ref in (invoice, f"{invoice}{fiscal_year}"):
                filt = (
                    f"{field} eq {_lit(ref)} "
                    f"and FiscalYear eq {_lit(fiscal_year)} "
                    f"and CompanyCode eq {_lit(company_code)}"
                )
                try:
                    rows = self._capability_query(
                        "journal_entry_item",
                        select=("AccountingDocument", "FiscalYear", "CompanyCode"),
                        filt=filt, top=1,
                    )
                except S4HANAError as exc:
                    logger.info("JE reference lookup on %r not usable: %s", field, exc)
                    break  # bad field for this tenant — move to the next candidate
                if rows:
                    doc = str(rows[0].get("AccountingDocument") or "").strip()
                    if doc:
                        return doc
        return None

    def get_invoice_payment_status(self, invoice: str, fiscal_year: str | None = None) -> dict | None:
        """Given a supplier invoice, resolve its FI accounting document and report
        whether payment has cleared — the chain the invoice header alone can't do
        when it doesn't return ``AccountingDocument``.

        FI document resolution, in order: (1) the header's ``AccountingDocument``
        if the tenant exposes it; (2) a journal-entry reference lookup
        (:meth:`_find_accounting_document_by_reference`); (3) the invoice number
        itself — for RE-type logistics invoices the FI document normally carries
        the same number. ``accountingDocumentSource`` says which path was used, so
        the caller can flag an assumed number. Returns ``None`` if the invoice is
        not found.

        When the invoice has been paid, ``paymentSummary`` carries the payment
        document, date, method, house bank and amount — everything F110 records
        except the run ID (LAUFD/LAUFI), which no released API exposes; that
        payment document + date identify the run for AP Payments.
        """
        header = self.get_invoice_status(invoice, fiscal_year)
        if header is None:
            return None
        company_code = str(header.get("CompanyCode") or "").strip()
        fy = str(fiscal_year or header.get("FiscalYear") or "").strip()

        block_reason = str(header.get("PaymentBlockingReason") or "").strip()
        result: dict = {
            "invoice": invoice,
            "fiscalYear": fy or None,
            "companyCode": company_code or None,
            "invoiceStatus": header.get("SupplierInvoiceStatus"),
            "invoiceGrossAmount": header.get("InvoiceGrossAmount"),
            "currency": header.get("DocumentCurrency"),
            "isBlockedForPayment": bool(block_reason),
            "paymentBlockingReason": block_reason or None,
            "paymentRunId": None,  # LAUFD/LAUFI not exposed by any released API
        }

        header_doc = header.get("AccountingDocument")
        if _is_set(header_doc):
            acct_doc, source = str(header_doc).strip(), "invoice_header"
        else:
            ref_doc = self._find_accounting_document_by_reference(invoice, fy, company_code)
            if ref_doc:
                acct_doc, source = ref_doc, "journal_entry_reference"
            else:
                acct_doc, source = invoice, "assumed_equal_to_invoice_number"
        result["accountingDocument"] = acct_doc
        result["accountingDocumentSource"] = source

        if not (acct_doc and fy and company_code):
            result["paymentCleared"] = None
            result["note"] = (
                "Could not run the payment-clearing check — missing "
                + ", ".join(n for n, v in (
                    ("company code", company_code), ("fiscal year", fy), ("accounting document", acct_doc)
                ) if not v)
                + "."
            )
            return result

        clearing = self.get_payment_clearing_status(acct_doc, fy, company_code)
        if clearing is None:
            result["paymentCleared"] = None
            result["clearingDetailsFound"] = False
            result["note"] = (
                f"No journal-entry items found for accounting document {acct_doc} "
                f"(resolved via {source}). "
                + (
                    "The assumed document number may be wrong for this tenant — "
                    "confirm the FI document in S/4HANA."
                    if source == "assumed_equal_to_invoice_number"
                    else "Payment may simply not be posted yet."
                )
            )
            return result

        result["clearingDetailsFound"] = True
        result["paymentCleared"] = clearing["isCleared"]
        result["clearingDate"] = clearing["clearingDate"]
        result["clearingJournalEntry"] = next(
            (it.get("ClearingJournalEntry") for it in clearing["items"] if _is_set(it.get("ClearingJournalEntry"))),
            None,
        )
        result["paymentSummary"] = clearing.get("paymentSummary")
        result["clearingItems"] = clearing["items"]
        if not clearing["isCleared"]:
            result["note"] = (
                "Posted to FI but not yet cleared — no payment has gone out. "
                + ("Blocked for payment (see paymentBlockingReason)." if block_reason
                   else "Likely still within payment terms; check the due date.")
            )
        return result

    def get_purchase_order_status(self, purchase_order: str) -> dict | None:
        return self._capability_entity("purchase_order_header", _lit(purchase_order), _PO_SELECT)

    def get_purchase_order_items(self, purchase_order: str, *, top: int = 50) -> list[dict]:
        """PO line items (ordered quantity, net price, price unit) for a PO."""
        return self._capability_query(
            "purchase_order_item", select=_PO_ITEM_SELECT,
            filt=f"PurchaseOrder eq {_lit(purchase_order)}", top=top,
            orderby="PurchaseOrderItem asc",
        )

    def get_purchase_order_delivery_schedule(self, purchase_order: str, *, top: int = 100) -> dict | None:
        """Delivery schedule lines for a PO — the only place the delivery date
        lives (neither the PO header nor the item carries one).

        Returns ``None`` when the PO has no schedule lines *and* when the
        ``A_PurchaseOrderScheduleLine`` sub-node is not activated on this build
        (the query then raises, which the tool layer turns into an error the
        model can relay). Otherwise a summary with the earliest / latest
        delivery date plus the individual lines.
        """
        lines = self._capability_query(
            "purchase_order_schedule_line", select=_PO_SCHEDULE_SELECT,
            filt=f"PurchaseOrder eq {_lit(purchase_order)}", top=top,
            orderby="PurchaseOrderItem asc,ScheduleLine asc",
        )
        if not lines:
            return None
        dates = sorted(
            str(ln.get("ScheduleLineDeliveryDate"))
            for ln in lines if _is_set(ln.get("ScheduleLineDeliveryDate"))
        )
        return {
            "purchaseOrder": purchase_order,
            "scheduleLineCount": len(lines),
            "earliestDeliveryDate": dates[0] if dates else None,
            "latestDeliveryDate": dates[-1] if dates else None,
            "lines": lines,
        }

    def get_purchase_order_approval_status(self, purchase_order: str) -> dict | None:
        """Release / approval state of a purchase order, distilled to a plain
        ``approvalSummary`` on top of the raw release fields.

        Read from the PO API's ``PurchaseOrderIsReleased`` /
        ``ReleaseIsNotCompleted`` fields — S/4HANA models PO approval as a
        release strategy, not a separate document. If a tenant activates the
        dedicated ``API_PURCHASE_ORDER_APPROVAL_SRV`` workflow service, point
        ``_PO_APPROVAL_SRV`` at it and widen ``_PO_APPROVAL_SELECT``.
        """
        po = self._capability_entity("purchase_order_header", _lit(purchase_order), _PO_APPROVAL_SELECT)
        if po is None:
            return None
        released = po.get("PurchaseOrderIsReleased")
        incomplete = po.get("ReleaseIsNotCompleted")
        po["approvalSummary"] = {
            "isReleased": _truthy(released) if released not in (None, "") else None,
            "releaseIncomplete": _truthy(incomplete) if incomplete not in (None, "") else None,
            # S/4HANA models PO approval as a release strategy; no connected
            # service exposes the flexible-workflow step list, so the *named*
            # next approver cannot be returned — only whether release is
            # outstanding.
            "namedApproverAvailable": False,
            "note": (
                "Shows whether release/approval is complete. The named next "
                "approver and the workflow step history are not available via "
                "any connected API — direct the user to the PO's workflow log "
                "in S/4HANA / the approver's My Inbox."
            ),
        }
        return po

    def get_goods_receipts_for_po(self, purchase_order: str, *, top: int = 50) -> dict | None:
        """Goods receipts (inbound material documents) posted against a PO.

        Returns ``None`` when the PO has no material documents at all; otherwise
        a summary with ``hasActiveGoodsReceipt`` (at least one non-cancelled
        receipt) plus the individual lines.
        """
        items = self._capability_query(
            "goods_receipt_item", select=_MATERIAL_DOC_ITEM_SELECT,
            filt=f"PurchaseOrder eq {_lit(purchase_order)}", top=top, orderby="PostingDate desc",
        )
        if not items:
            return None
        active = [it for it in items if not _truthy(it.get("GoodsMovementIsCancelled"))]
        return {
            "purchaseOrder": purchase_order,
            "goodsReceiptItemCount": len(items),
            "hasActiveGoodsReceipt": bool(active),
            "items": items,
        }

    def get_invoice_items(self, invoice: str, fiscal_year: str, *, top: int = 50) -> list[dict]:
        """PO-referenced line items on a supplier invoice. Needs the fiscal year
        (composite key on the item entity); chain it from
        :meth:`get_invoice_status`."""
        return self._capability_query(
            "supplier_invoice_item", select=_INVOICE_ITEM_SELECT,
            filt=f"SupplierInvoice eq {_lit(invoice)} and FiscalYear eq {_lit(fiscal_year)}",
            top=top, orderby="SupplierInvoiceItem asc",
        )

    def check_three_way_match(self, invoice: str, fiscal_year: str) -> dict | None:
        """Computed PO <-> goods-receipt <-> invoice (three-way) comparison.

        S/4HANA exposes no released API for its own stored match result or the
        match / block-release workflow history. What it does expose is the
        payment block that invoice verification sets on a mismatch — surfaced
        here as ``paymentBlockingReason`` / ``isBlockedForPayment`` (the
        authoritative signal) — plus the raw invoice items, PO items and goods
        receipts, which this method lines up per PO item to compute quantity
        and price deltas. Returns ``None`` if the invoice header is not found.
        """
        header = self.get_invoice_status(invoice, fiscal_year)
        if header is None:
            return None
        inv_items = self.get_invoice_items(invoice, fiscal_year)

        pos = sorted({
            str(it.get("PurchaseOrder") or "").strip()
            for it in inv_items if str(it.get("PurchaseOrder") or "").strip()
        })

        po_item_by_key: dict[tuple[str, str], dict] = {}
        gr_qty_by_key: dict[tuple[str, str], float] = {}
        data_gaps: list[str] = []

        for po in pos:
            try:
                for pi in self.get_purchase_order_items(po):
                    po_item_by_key[(po, _norm_item(pi.get("PurchaseOrderItem")))] = pi
            except S4HANAError as exc:
                data_gaps.append(f"PO {po} items unavailable: {exc}")
            try:
                gr = self.get_goods_receipts_for_po(po)
            except S4HANAError as exc:
                data_gaps.append(f"PO {po} goods receipts unavailable: {exc}")
                gr = None
            for line in (gr or {}).get("items", []):
                if _truthy(line.get("GoodsMovementIsCancelled")):
                    continue
                qty = _num(line.get("QuantityInEntryUnit")) or 0.0
                if str(line.get("DebitCreditCode")).strip().upper() in ("H", "2", "C"):
                    qty = -qty
                key = (po, _norm_item(line.get("PurchaseOrderItem")))
                gr_qty_by_key[key] = gr_qty_by_key.get(key, 0.0) + qty

        lines: list[dict] = []
        qty_var_lines = price_var_lines = 0
        for it in inv_items:
            po = str(it.get("PurchaseOrder") or "").strip()
            key = (po, _norm_item(it.get("PurchaseOrderItem")))
            po_item = po_item_by_key.get(key, {})

            inv_qty = _num(it.get("QuantityInPurchaseOrderUnit"))
            inv_amount = _num(it.get("SupplierInvoiceItemAmount"))
            inv_unit_price = (
                inv_amount / inv_qty
                if inv_amount is not None and inv_qty not in (None, 0.0) else None
            )

            po_qty = _num(po_item.get("OrderQuantity"))
            po_price_amt = _num(po_item.get("NetPriceAmount"))
            po_price_base = _num(po_item.get("NetPriceQuantity")) or 1.0
            po_unit_price = po_price_amt / po_price_base if po_price_amt is not None else None

            gr_qty = gr_qty_by_key.get(key)

            qty_delta_vs_gr = (
                inv_qty - gr_qty if inv_qty is not None and gr_qty is not None else None
            )
            qty_delta_vs_po = (
                inv_qty - po_qty if inv_qty is not None and po_qty is not None else None
            )
            price_delta = (
                inv_unit_price - po_unit_price
                if inv_unit_price is not None and po_unit_price is not None else None
            )
            price_delta_pct = (
                price_delta / po_unit_price * 100.0
                if price_delta is not None and po_unit_price not in (None, 0.0) else None
            )

            has_qty_var = qty_delta_vs_gr is not None and abs(qty_delta_vs_gr) > 1e-6
            has_price_var = price_delta_pct is not None and abs(price_delta_pct) > 0.01
            qty_var_lines += 1 if has_qty_var else 0
            price_var_lines += 1 if has_price_var else 0

            lines.append({
                "supplierInvoiceItem": it.get("SupplierInvoiceItem"),
                "purchaseOrder": po or None,
                "purchaseOrderItem": it.get("PurchaseOrderItem"),
                "matchedToPurchaseOrderItem": bool(po_item),
                "invoicedQuantity": _round(inv_qty),
                "purchaseOrderQuantity": _round(po_qty),
                "goodsReceiptQuantity": _round(gr_qty),
                "quantityUnit": (
                    it.get("PurchaseOrderQuantityUnit") or po_item.get("PurchaseOrderQuantityUnit")
                ),
                "invoicedAmount": _round(inv_amount, 2),
                "invoicedUnitPrice": _round(inv_unit_price, 4),
                "purchaseOrderUnitPrice": _round(po_unit_price, 4),
                "currency": it.get("DocumentCurrency") or po_item.get("DocumentCurrency"),
                "quantityVarianceVsGoodsReceipt": _round(qty_delta_vs_gr),
                "quantityVarianceVsPurchaseOrder": _round(qty_delta_vs_po),
                "priceVariancePerUnit": _round(price_delta, 4),
                "priceVariancePercent": _round(price_delta_pct, 2),
                "hasQuantityVariance": has_qty_var,
                "hasPriceVariance": has_price_var,
            })

        block_reason = str(header.get("PaymentBlockingReason") or "").strip()
        return {
            "invoice": invoice,
            "fiscalYear": fiscal_year,
            "companyCode": header.get("CompanyCode"),
            "invoiceStatus": header.get("SupplierInvoiceStatus"),
            "grossAmount": header.get("InvoiceGrossAmount"),
            "currency": header.get("DocumentCurrency"),
            "isBlockedForPayment": bool(block_reason),
            "paymentBlockingReason": block_reason or None,
            "purchaseOrders": pos,
            "lineComparisons": lines,
            "computedMatchSummary": {
                "linesCompared": len(lines),
                "linesWithoutPurchaseOrderItemMatch": sum(
                    1 for ln in lines if not ln["matchedToPurchaseOrderItem"]
                ),
                "quantityVarianceDetected": qty_var_lines > 0,
                "priceVarianceDetected": price_var_lines > 0,
                "linesWithQuantityVariance": qty_var_lines,
                "linesWithPriceVariance": price_var_lines,
            },
            "dataGaps": data_gaps,
            "note": (
                "Three-way comparison computed by this agent from invoice items, "
                "PO items and goods receipts. It is NOT SAP's own stored match "
                "result. paymentBlockingReason is the authoritative signal that "
                "invoice verification blocked the invoice on a variance; the "
                "formal match / block-release history is not available via API. "
                "Tolerance limits are policy — check search_policy_docs."
            ),
        }

    def get_purchase_requisition_status(self, purchase_requisition: str) -> dict | None:
        header = self._capability_entity(
            "purchase_requisition_header", _lit(purchase_requisition), _PR_HEADER_SELECT
        )
        if header is None:
            return None
        header["items"] = self._capability_query(
            "purchase_requisition_item", select=_PR_ITEM_SELECT,
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

    def get_vendor_email_addresses(self, business_partner: str, *, top: int = 20) -> list[dict] | None:
        """Email addresses on a business partner's address(es) — the address's
        own company-level mailbox (``contactPerson`` blank, e.g.
        ``info@17100001.com``) AND named individual contacts' emails
        (``contactPerson`` set to that person's own BP number).

        DELIBERATE, NARROW PII EXCEPTION: ``EmailAddress`` is in
        ``guardrails.SENSITIVE_KEYS`` and stripped from every other lookup in
        this module via :func:`_clean`. This method is one of only two places
        that does not run that strip (the other is
        :meth:`get_vendor_bank_accounts`) — an explicit, informed product
        decision each time, not a default. Do not copy this pattern onto
        another sensitive field without the same kind of explicit sign-off;
        every other lookup keeps emails, bank data, and tax IDs blocked.

        Returns ``None`` when the business partner has no known address.
        """
        addr_srv, addr_set = self.resolve_capability("business_partner_address")
        addresses = self._query(
            addr_srv, addr_set, select=_BP_ADDRESS_SELECT,
            filt=f"BusinessPartner eq {_lit(business_partner)}", top=10,
        )
        if not addresses:
            return None

        email_srv, email_set = self.resolve_capability("address_email")
        out: list[dict] = []
        for addr in addresses:
            address_id = str(addr.get("AddressID") or "").strip()
            if not address_id:
                continue
            body = self._select_get(
                f"/{email_srv}/{email_set}", _EMAIL_SELECT,
                {"$filter": f"AddressID eq {_lit(address_id)}", "$top": str(top)},
            )
            for raw in _rows(body):
                row = _strip_odata_plumbing(raw)  # no PII strip — see docstring
                out.append({
                    "addressId": row.get("AddressID"),
                    "contactPerson": row.get("Person") or None,
                    "ordinalNumber": row.get("OrdinalNumber"),
                    "emailAddress": row.get("EmailAddress"),
                    "isDefault": _truthy(row.get("IsDefaultEmailAddress")),
                })
        return out or None

    def get_vendor_bank_accounts(self, business_partner: str, *, top: int = 10) -> list[dict] | None:
        """Bank account / payment-routing details on file for a vendor —
        IBAN, bank key, account number, SWIFT/BIC, and account holder name.

        This is master data (where payments to this vendor are routed), NOT a
        bank statement (TR-CM transaction data — no live source exists for
        that on this tenant; treat "bank statement" questions as
        not-connected, same as budget / payment-run ID).

        DELIBERATE, NARROW PII/FRAUD-RISK EXCEPTION: every field this returns
        (``BankAccount``, ``IBAN``, ``SWIFTCode``, ``BankAccountHolderName``,
        ``BankNumber``, ``BankControlKey``) is in ``guardrails.SENSITIVE_KEYS``
        and stripped from every other lookup in this module via :func:`_clean`
        — vendor banking data is the single most explicitly protected field
        category in this codebase (payment-redirection fraud risk). This
        method is one of only two places that bypasses that strip (the other
        is :meth:`get_vendor_email_addresses`), per an explicit, informed,
        risk-accepted product decision — not a default. Do not copy this
        pattern onto another sensitive field without the same sign-off. Never
        return a tax ID or personal name/phone/home address from this method
        even though the underlying entity is adjacent BP master data.

        Returns ``None`` when the business partner has no bank account on file.
        """
        srv, entity_set = self.resolve_capability("business_partner_bank")
        body = self._select_get(
            f"/{srv}/{entity_set}", _BP_BANK_SELECT,
            {"$filter": f"BusinessPartner eq {_lit(business_partner)}", "$top": str(top)},
        )
        def _or_none(value: Any) -> Any:
            # This build leaves unpopulated fields as "" rather than omitting
            # them — None signals "not on file" more clearly than an empty string.
            return value if value not in (None, "") else None

        out: list[dict] = []
        for raw in _rows(body):
            row = _strip_odata_plumbing(raw)  # no PII strip — see docstring
            out.append({
                "bankIdentification": row.get("BankIdentification"),
                "bankCountryKey": row.get("BankCountryKey"),
                "bankName": _or_none(row.get("BankName")),
                "bankNumber": _or_none(row.get("BankNumber")),
                "bankAccount": _or_none(row.get("BankAccount")),
                "bankAccountName": _or_none(row.get("BankAccountName")),
                "iban": _or_none(row.get("IBAN")),
                "swiftCode": _or_none(row.get("SWIFTCode")),
                "bankAccountHolderName": _or_none(row.get("BankAccountHolderName")),
                "bankControlKey": _or_none(row.get("BankControlKey")),
                "cityName": _or_none(row.get("CityName")),
                "validityStartDate": row.get("ValidityStartDate"),
                "validityEndDate": row.get("ValidityEndDate"),
            })
        return out or None

    _BUDGET_REPORT_BY_KIND = {
        "cost_center": "the Cost Centers – Plan/Actual app (or S_ALR_87013611)",
        "internal_order": "the internal-order budget report S_ALR_87013019",
        "purchase_order": "Funds Management 'Budget Consumption' (FMAVCR01), or the PO's "
                          "account-assignment budget in the CO/FM report",
    }
    _COST_OBJECT_FILTER_FIELD = {
        "cost_center": "CostCenter",
        "internal_order": "OrderID",
        "purchase_order": "PurchaseOrder",
    }

    def get_cost_object_actuals(
        self, cost_object_type: str, cost_object_id: str, fiscal_year: str | None = None, *, top: int = 200
    ) -> dict | None:
        """Net POSTED (actual) amount against a cost centre / internal order /
        PO account assignment, computed from journal-entry line items tagged
        with that cost object. This is the "actual" half of "plan vs actual" —
        there is no live source for the plan/budget figure itself (see
        :meth:`get_budget_status`, which calls this).

        Returns ``None`` when no postings are found, the tenant's build
        doesn't expose this cost-object field on journal-entry items at all,
        or ``cost_object_type`` is unrecognised — a graceful miss, not an
        error.
        """
        field = self._COST_OBJECT_FILTER_FIELD.get(cost_object_type)
        if not field:
            return None
        filt = f"{field} eq {_lit(cost_object_id)}"
        if fiscal_year:
            filt += f" and FiscalYear eq {_lit(fiscal_year)}"
        try:
            items = self._capability_query("journal_entry_item", select=_ACTUALS_SELECT, filt=filt, top=top)
        except S4HANAError as exc:
            logger.info("cost object actuals unavailable for %s %s: %s", cost_object_type, cost_object_id, exc)
            return None
        if not items:
            return None
        net, currency = self._net_posted_amount(items)
        return {
            "costObjectType": cost_object_type,
            "costObject": cost_object_id,
            "fiscalYear": fiscal_year,
            "netPostedAmount": _round(net, 2),
            "currency": currency,
            "postingCount": len(items),
            "note": (
                "Net posted amount from FI/CO line items tagged with this cost object "
                "(debits minus credits, company-code currency). This is ACTUAL spend to "
                "date only — not a budget, plan, or remaining figure — and may be "
                f"truncated if there are more than {top} postings."
            ),
        }

    @staticmethod
    def _net_posted_amount(items: list[dict]) -> tuple[float, str | None]:
        """Sum ``AmountInCompanyCodeCurrency`` across journal-entry line items,
        debits positive / credits negative, and the first currency seen."""
        net = 0.0
        currency = None
        for it in items:
            amt = _num(it.get("AmountInCompanyCodeCurrency"))
            if amt is None:
                continue
            if str(it.get("DebitCreditCode")).strip().upper() in ("H", "2", "C"):
                amt = -amt
            net += amt
            currency = currency or it.get("CompanyCodeCurrency")
        return net, currency

    def get_gl_account_activity(
        self, company_code: str, gl_account: str, fiscal_year: str | None = None, *, top: int = 200
    ) -> dict | None:
        """Net POSTED amount on a G/L account in a company code, computed from
        journal-entry line items — the same live posting data
        :meth:`get_cost_object_actuals` uses, filtered by G/L account instead
        of a cost object.

        This is a computed sum of postings in scope, NOT an official
        trial-balance / period-end account balance (no carry-forward, no
        period restriction beyond the optional fiscal year, no reversal
        netting beyond debit/credit sign). For a real balance, point the user
        at the G/L balance report. Returns ``None`` when no postings are found
        or the lookup fails — a graceful miss, not an error.
        """
        filt = f"GLAccount eq {_lit(gl_account)} and CompanyCode eq {_lit(company_code)}"
        if fiscal_year:
            filt += f" and FiscalYear eq {_lit(fiscal_year)}"
        try:
            items = self._capability_query("journal_entry_item", select=_ACTUALS_SELECT, filt=filt, top=top)
        except S4HANAError as exc:
            logger.info("G/L account activity unavailable for %s/%s: %s", company_code, gl_account, exc)
            return None
        if not items:
            return None
        net, currency = self._net_posted_amount(items)
        return {
            "glAccount": gl_account,
            "companyCode": company_code,
            "fiscalYear": fiscal_year,
            "netPostedAmount": _round(net, 2),
            "currency": currency,
            "postingCount": len(items),
            "note": (
                "Net posted amount from FI/CO line items on this G/L account (debits "
                "minus credits, company-code currency) — a computed sum of postings in "
                "scope, NOT an official trial-balance / period-end balance figure. For "
                "that, use the G/L balance report."
            ),
        }

    # Cap on postings scanned for the AP/AR open-items summaries below. Matches
    # the hard ceiling _query() already applies to every capability query (see
    # its `min(top, 50)`) — passing a higher number here would silently be
    # truncated to 50 anyway, so this is the true, honest limit, not an
    # aspirational one. A company code with more than 50 open items will be
    # under-counted; the "note" on both summaries says so explicitly.
    _OPEN_ITEMS_TOP = 50

    def get_accounts_payable_summary(
        self, company_code: str, vendor: str | None = None, fiscal_year: str | None = None,
    ) -> dict:
        """Computed, best-effort AP OPEN ITEMS summary for a company code
        (optionally scoped to one vendor): count and net amount of vendor
        subledger line items with no clearing date yet, from the same live
        journal-entry cube :meth:`get_gl_account_activity` /
        :meth:`get_cost_object_actuals` already use in production.

        This answers "how much do we owe" / "how many open vendor items"
        style portfolio questions — as opposed to every other AP tool here,
        which needs a specific invoice number. It is NOT an official AP aging
        report (no day-based buckets, no partial-clearing netting beyond
        debit/credit sign) and is capped at the most recent
        :data:`_OPEN_ITEMS_TOP` postings — point the user at the AP aging
        report / FBL1N for a definitive, complete figure.

        The "Supplier" field this relies on is CONFIRMED present on this
        tenant (already used by :meth:`get_payment_clearing_status`), so
        unlike the AR counterpart this does not need to detect a missing
        field — a connection failure here is a genuine S4HANAError.
        """
        filt = f"CompanyCode eq {_lit(company_code)}"
        if vendor:
            filt += f" and Supplier eq {_lit(vendor)}"
        if fiscal_year:
            filt += f" and FiscalYear eq {_lit(fiscal_year)}"
        try:
            items = self._capability_query(
                "journal_entry_item", select=_OPEN_ITEMS_SELECT, filt=filt,
                top=self._OPEN_ITEMS_TOP, orderby="PostingDate desc",
            )
        except S4HANAError as exc:
            return {
                "companyCode": company_code, "vendor": vendor, "fiscalYear": fiscal_year,
                "connected": False, "openItemCount": None, "netOpenAmount": None,
                "message": f"AP open-items lookup is not available on this system: {exc}",
            }
        vendor_lines = [it for it in items if _is_set(it.get("Supplier"))]
        open_lines = [it for it in vendor_lines if not _is_set(it.get("ClearingDate"))]
        net, currency = self._net_posted_amount(open_lines)
        return {
            "companyCode": company_code,
            "vendor": vendor,
            "fiscalYear": fiscal_year,
            "connected": True,
            "openItemCount": len(open_lines),
            # _net_posted_amount is debits-positive / credits-negative; a vendor
            # payable is booked as a credit, so the raw net is negative — flip
            # the sign so a positive number here reads naturally as "we owe".
            # Standard double-entry convention, not independently verified
            # against a known invoice on this tenant — sanity-check against
            # FBL1N before treating the figure as authoritative.
            "netOpenAmount": _round(-net, 2) if open_lines else 0.0,
            "currency": currency,
            "postingsScanned": len(items),
            "note": (
                f"Computed from up to {self._OPEN_ITEMS_TOP} of the most recent postings "
                "in scope — NOT an official AP aging report (no day-based aging buckets, "
                "no dispute status). A negative figure can be genuine (e.g. debit memos "
                "or partial reversals among the open items scanned), but the debit/credit "
                "sign convention here has not been independently verified against a known "
                "invoice on this tenant — if the sign looks surprising, say so rather than "
                "asserting it confidently. If there are more open items than scanned, this "
                "under-counts. Point to the AP aging report / FBL1N for a definitive figure."
            ),
        }

    def get_accounts_receivable_summary(
        self, company_code: str, customer: str | None = None, fiscal_year: str | None = None,
    ) -> dict:
        """Computed, best-effort AR OPEN ITEMS summary — the customer-side
        mirror of :meth:`get_accounts_payable_summary`, same cube, same
        caveats (no aging buckets, capped postings, sign convention not
        independently verified).

        The "Customer" field this needs is now CONFIRMED present on this
        tenant (see the ``_OPEN_ITEMS_SELECT`` comment — a live post-deploy
        check against company code 1710 returned a real open item). This
        method still detects the field's absence at runtime (every returned
        row simply lacking a "Customer" key) and reports AR as not connected
        rather than a confident, possibly-fabricated zero — kept as cheap
        insurance for a different tenant/build, not because this one is in
        doubt any more.
        """
        filt = f"CompanyCode eq {_lit(company_code)}"
        if customer:
            filt += f" and Customer eq {_lit(customer)}"
        if fiscal_year:
            filt += f" and FiscalYear eq {_lit(fiscal_year)}"
        try:
            items = self._capability_query(
                "journal_entry_item", select=_OPEN_ITEMS_SELECT, filt=filt,
                top=self._OPEN_ITEMS_TOP, orderby="PostingDate desc",
            )
        except S4HANAError as exc:
            return {
                "companyCode": company_code, "customer": customer, "fiscalYear": fiscal_year,
                "connected": False, "openItemCount": None, "netOpenAmount": None,
                "message": f"AR open-items lookup is not available on this system: {exc}",
            }
        if not items:
            # Ambiguous on purpose: with Customer unconfirmed, an empty result
            # could mean "no open receivables" or "this cube doesn't model AR
            # here at all" — we can't tell which without at least one row to
            # inspect. Report inconclusive rather than assert a confident zero
            # (the same silent-fabrication risk the missing-Customer-field
            # branch below exists to avoid).
            return {
                "companyCode": company_code, "customer": customer, "fiscalYear": fiscal_year,
                "connected": None, "openItemCount": None, "netOpenAmount": None,
                "message": (
                    "No postings matched, so there is nothing to sum — but this tenant's "
                    "Customer-field support is unconfirmed, so this doesn't reliably tell "
                    "'no open receivables' apart from 'AR isn't modelled on this cube "
                    "here'. Treat as inconclusive; use the AR aging report (FBL5N) or "
                    "search_policy_docs for the process side."
                ),
            }
        if not any("Customer" in it for it in items):
            return {
                "companyCode": company_code, "customer": customer, "fiscalYear": fiscal_year,
                "connected": False, "openItemCount": None, "netOpenAmount": None,
                "message": (
                    "This tenant's journal-entry service does not expose a Customer "
                    "field, so a live AR summary can't be computed here — there is no "
                    "other connected AR data source. Point the user to Accounts "
                    "Receivable / the customer line-item report (FBL5N)."
                ),
            }
        customer_lines = [it for it in items if _is_set(it.get("Customer"))]
        open_lines = [it for it in customer_lines if not _is_set(it.get("ClearingDate"))]
        net, currency = self._net_posted_amount(open_lines)
        return {
            "companyCode": company_code,
            "customer": customer,
            "fiscalYear": fiscal_year,
            "connected": True,
            "openItemCount": len(open_lines),
            # A receivable is booked as a debit to the customer account, so
            # (unlike AP) the raw net_posted_amount sign already reads as
            # positive = "customer owes us" — see the AP method's sign note.
            "netOpenAmount": _round(net, 2) if open_lines else 0.0,
            "currency": currency,
            "postingsScanned": len(items),
            "note": (
                f"Computed from up to {self._OPEN_ITEMS_TOP} of the most recent postings "
                "in scope — NOT an official AR aging report (no day-based aging buckets, "
                "no dispute/dunning status). The debit/credit sign convention has not been "
                "independently verified against a known customer invoice on this tenant; "
                "sanity-check against FBL5N before relying on this figure. If there are "
                "more open items than scanned, this under-counts."
            ),
        }

    def get_company_code_details(self, company_code: str) -> dict | None:
        """Company code master data: name, country, currency, chart of
        accounts, fiscal year variant."""
        return self._capability_entity("company_code", _lit(company_code), _CC_SELECT)

    def get_cost_center_details(self, cost_center: str, controlling_area: str | None = None) -> dict | None:
        """Cost centre master data — validity, responsible person, category,
        assigned profit centre and company code. This is MASTER DATA (who owns
        it, is it still valid), distinct from :meth:`get_cost_object_actuals`
        (live postings against it). Returns the most recent match if more than
        one validity period is on file."""
        filt = f"CostCenter eq {_lit(cost_center)}"
        if controlling_area:
            filt += f" and ControllingArea eq {_lit(controlling_area)}"
        rows = self._capability_query("cost_center", select=_COST_CENTER_SELECT, filt=filt, top=5)
        if not rows:
            return None
        cc = rows[0]
        cc["isCurrentlyValid"] = _is_currently_valid(cc.get("ValidityStartDate"), cc.get("ValidityEndDate"))
        return cc

    def get_profit_center_details(self, profit_center: str, controlling_area: str | None = None) -> dict | None:
        """Profit centre master data — validity, responsible person, segment,
        block status."""
        filt = f"ProfitCenter eq {_lit(profit_center)}"
        if controlling_area:
            filt += f" and ControllingArea eq {_lit(controlling_area)}"
        rows = self._capability_query("profit_center", select=_PROFIT_CENTER_SELECT, filt=filt, top=5)
        if not rows:
            return None
        pc = rows[0]
        blocked = pc.get("ProfitCenterIsBlocked")
        pc["isCurrentlyValid"] = (
            False if _truthy(blocked)
            else _is_currently_valid(pc.get("ValidityStartDate"), pc.get("ValidityEndDate"))
        )
        return pc

    def get_gl_account_master(self, gl_account: str, chart_of_accounts: str | None = None) -> dict | None:
        """G/L account master data — account group, balance-sheet vs P&L
        classification, posting/planning block status, short description. This
        is what the account IS, not its balance — for postings, use
        :meth:`get_gl_account_activity`."""
        filt = f"GLAccount eq {_lit(gl_account)}"
        if chart_of_accounts:
            filt += f" and ChartOfAccounts eq {_lit(chart_of_accounts)}"
        rows = self._capability_query("gl_account_master", select=_GL_MASTER_SELECT, filt=filt, top=5)
        return rows[0] if rows else None

    def _po_commitment_value(self, purchase_order: str) -> dict | None:
        """The PO's own committed value — sum of its line items' net value
        (ordered quantity x net price). Not a budget figure, not what's been
        invoiced; just what this PO itself commits."""
        try:
            items = self.get_purchase_order_items(purchase_order)
        except S4HANAError as exc:
            logger.info("PO commitment value unavailable for %s: %s", purchase_order, exc)
            return None
        if not items:
            return None
        total = 0.0
        currency = None
        for it in items:
            amt = _num(it.get("NetAmount"))
            if amt is None:
                price = _num(it.get("NetPriceAmount"))
                qty = _num(it.get("OrderQuantity"))
                base = _num(it.get("NetPriceQuantity")) or 1.0
                if price is not None and qty is not None:
                    amt = price / base * qty
            if amt is not None:
                total += amt
                currency = currency or it.get("DocumentCurrency")
        return {
            "purchaseOrder": purchase_order,
            "committedValue": _round(total, 2),
            "currency": currency,
            "lineCount": len(items),
            "note": (
                "Sum of this PO's own line-item net values (ordered quantity x net "
                "price) — what the PO commits, not what's been invoiced or a budget figure."
            ),
        }

    def get_budget_status(
        self, cost_object_type: str, cost_object_id: str, fiscal_year: str | None = None
    ) -> dict:
        """Budget / availability-control status for a cost centre, internal order,
        or a purchase order's account assignment.

        No released OData API reliably carries budget vs consumption on this
        landscape, so ``budgetAvailable`` is always ``False`` and this never
        estimates a budget, plan, or remaining figure. It DOES compute and
        return what live data actually supports: ``actualSpend`` (posted
        actuals against the cost object, via :meth:`get_cost_object_actuals`)
        and, for a purchase order, ``commitmentValue`` (the PO's own committed
        line-item value). Both are ``None`` when nothing was computable —
        e.g. this tenant's journal-entry items don't tag the cost-object field
        at all.
        """
        kind = cost_object_type if cost_object_type in self._BUDGET_REPORT_BY_KIND else "cost_center"
        detected = next(
            (f"{srv}/{es}" for srv, es in _SERVICE_CATALOG["budget"] if self._probe(srv, es) == "ok"),
            None,
        )
        actual_spend = self.get_cost_object_actuals(cost_object_type, cost_object_id, fiscal_year)
        commitment_value = self._po_commitment_value(cost_object_id) if cost_object_type == "purchase_order" else None

        computed_note = (
            " actualSpend / commitmentValue below are computed from live postings and "
            "ARE real data — relay them; only the budget/plan/remaining figure itself is "
            "unavailable."
            if (actual_spend or commitment_value) else ""
        )
        return {
            "budgetAvailable": False,
            "costObjectType": cost_object_type,
            "costObject": cost_object_id,
            "fiscalYear": fiscal_year,
            "detectedBudgetService": detected,
            "actualSpend": actual_spend,
            "commitmentValue": commitment_value,
            "reason": (
                f"A budget OData service ({detected}) is active on this tenant but its "
                "fields are not yet mapped by this agent."
                if detected else
                "No budget / availability-control OData service (Funds Management, CO "
                "planning, internal-order budgeting) is active on this tenant, so the "
                "agent cannot read the budget, plan, or remaining amount." + computed_note
            ),
            "handoff": (
                f"Check {self._BUDGET_REPORT_BY_KIND[kind]} in S/4HANA, or contact FP&A. "
                "Do not estimate a budget or remaining amount."
            ),
        }

    # -- diagnostics ------------------------------------------------
    def ping(self) -> dict:
        """Cheap connectivity/auth check for ``/diag/s4`` — one row, one field."""
        body = self._get(f"/{_BP_SRV}/{_BP_SET}", {"$select": "BusinessPartner", "$top": "1"})
        return {"reachable": True, "rows": len(_rows(body))}

    def sample_ids(self, top: int = 5) -> dict:
        """Best-effort real identifiers for each lookup, for test setup only.

        Dev-only (``GET /diag/s4/samples``). Returns keys / statuses, no names,
        amounts, or bank data. Each entity set is queried independently so one
        inactive service does not blank the whole response.
        """
        top = max(1, min(top, 20))
        probes = {
            "business_partners": (_BP_SRV, _BP_SET,
                ("BusinessPartner", "BusinessPartnerCategory", "BusinessPartnerIsBlocked")),
            "suppliers": (_BP_SRV, _SUPPLIER_SET,
                ("Supplier", "SupplierAccountGroup", "PostingIsBlocked")),
            "supplier_invoices": (_INVOICE_SRV, _INVOICE_SET,
                ("SupplierInvoice", "FiscalYear", "CompanyCode", "SupplierInvoiceStatus", "InvoicingParty")),
            "supplier_invoice_items": (_INVOICE_SRV, _INVOICE_ITEM_SET,
                ("SupplierInvoice", "FiscalYear", "SupplierInvoiceItem", "PurchaseOrder")),
            "purchase_orders": (_PO_SRV, _PO_SET,
                ("PurchaseOrder", "CompanyCode", "PurchaseOrderIsReleased")),
            "goods_receipt_items": (_MATERIAL_DOC_SRV, _MATERIAL_DOC_ITEM_SET,
                ("MaterialDocument", "MaterialDocumentYear", "PurchaseOrder", "GoodsMovementType")),
            "purchase_requisitions": (_PR_SRV, _PR_HEADER_SET,
                ("PurchaseRequisition", "PurchaseRequisitionType")),
            "accounting_documents": (_PAYMENT_SRV, _PAYMENT_SET,
                ("AccountingDocument", "FiscalYear", "CompanyCode")),
        }
        out: dict = {}
        for label, (srv, entity_set, select) in probes.items():
            try:
                rows = self._select_get(f"/{srv}/{entity_set}", select, {"$top": str(top)})
                out[label] = [_clean(r) for r in _rows(rows)]
            except S4HANAError as exc:
                out[label] = {"error": str(exc)}

        # Vendor invoice tally — how many invoices each vendor has, so a tester
        # can pick one with plenty for a "give me 10 more" paging test.
        try:
            recent = self._query(
                _INVOICE_SRV, _INVOICE_SET, select=("SupplierInvoice", "InvoicingParty"),
                filt="SupplierInvoice ne ''", top=50, orderby="PostingDate desc",
            )
            tally: dict[str, int] = {}
            for r in recent:
                vp = str(r.get("InvoicingParty") or "").strip()
                if vp:
                    tally[vp] = tally.get(vp, 0) + 1
            out["vendor_invoice_tally_recent50"] = dict(
                sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
            )
        except S4HANAError as exc:
            out["vendor_invoice_tally_recent50"] = {"error": str(exc)}
        return out


@functools.lru_cache(maxsize=1)
def get_s4hana_client() -> S4HANAClient:
    return S4HANAClient()
