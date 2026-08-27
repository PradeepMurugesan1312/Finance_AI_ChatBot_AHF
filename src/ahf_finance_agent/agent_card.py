"""Agent Card — the identity and capability list an A2A client (Joule) reads
from ``/.well-known/agent.json`` to decide when to route a user request here.

The skills below mirror the in-scope capabilities from the design brief:
read-only S/4HANA status lookups for the five required OData services, plus
grounded finance-policy Q&A. Every skill is a *read*; there is deliberately no
skill that approves, posts, releases, or pays anything.

The skill implementations land in later build steps. Declaring them now keeps
the Joule-facing contract stable while the internals are filled in.
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

AGENT_NAME = "AHF Finance AI ChatBot"
AGENT_VERSION = "0.1.0"

_DESCRIPTION = (
    "Answers routine finance questions for AHF accounts-payable, procurement, "
    "and finance staff. Provides live read-only status of supplier invoices, "
    "payments, purchase orders, purchase requisitions, and vendor master data "
    "from SAP S/4HANA, and answers AP / procurement / finance policy and "
    "process questions grounded in approved company documents, with citations. "
    "Strictly read-only: it never approves, posts, releases, or pays anything, "
    "and never returns personal data or vendor banking details. Escalates to a "
    "human finance queue when it is not confident or the answer is not in the "
    "approved documents."
)


def _skills() -> list[AgentSkill]:
    return [
        AgentSkill(
            id="get_invoice_status",
            name="Get Invoice Status",
            description=(
                "Look up the status and header details of a specific supplier "
                "invoice in SAP S/4HANA by invoice number and fiscal year "
                "(payment terms, blocking reason, PO match)."
            ),
            tags=["accounts payable", "invoice", "status", "read-only"],
            examples=[
                "What's the status of invoice 5105601234 for fiscal year 2026?",
                "Has invoice 5105601234 been paid?",
            ],
        ),
        AgentSkill(
            id="search_invoices_by_vendor",
            name="Search Invoices by Vendor",
            description=(
                "List recent supplier invoices and their status for a given "
                "vendor in SAP S/4HANA."
            ),
            tags=["accounts payable", "invoice", "vendor", "read-only"],
            examples=[
                "Show me recent invoices from vendor 100000",
                "What invoices are open for supplier 100000 in company code 1000?",
            ],
        ),
        AgentSkill(
            id="get_payment_clearing_status",
            name="Get Payment Clearing Status",
            description=(
                "Confirm whether a payment has actually posted (cleared) "
                "against an accounting document in SAP S/4HANA, as opposed to "
                "the invoice merely being parked / posted / blocked."
            ),
            tags=["accounts payable", "payment", "clearing", "read-only"],
            examples=[
                "Has payment cleared for accounting document 1400000123, fiscal year 2026?",
                "Did we actually pay out accounting document 1400000123 yet?",
            ],
        ),
        AgentSkill(
            id="get_purchase_order_status",
            name="Get Purchase Order Status",
            description=(
                "Look up a purchase order's status, approval state, and header "
                "details in SAP S/4HANA."
            ),
            tags=["procurement", "purchase order", "status", "read-only"],
            examples=[
                "What's the status of PO 4500001234?",
                "Has purchase order 4500001234 been approved?",
            ],
        ),
        AgentSkill(
            id="get_purchase_requisition_status",
            name="Get Purchase Requisition Status",
            description=(
                "Look up a purchase requisition's status and header details in "
                "SAP S/4HANA."
            ),
            tags=["procurement", "purchase requisition", "status", "read-only"],
            examples=["What's the status of purchase requisition 1000005678?"],
        ),
        AgentSkill(
            id="get_vendor_details",
            name="Get Vendor Details",
            description=(
                "Look up a vendor's (business partner's) master data in SAP "
                "S/4HANA to confirm onboarding / setup status. Never returns "
                "bank account, IBAN, tax id, or personal contact details."
            ),
            tags=["vendor", "business partner", "onboarding", "read-only"],
            examples=[
                "Is vendor 100000 set up in the system?",
                "What's the onboarding status for business partner 100000?",
            ],
        ),
        AgentSkill(
            id="search_policy_docs",
            name="Answer Finance Policy Questions",
            description=(
                "Answer accounts-payable, procurement, and general finance "
                "policy / process questions (T&E rules, PO and PR approval "
                "thresholds, vendor onboarding steps, invoice payment terms "
                "and match tolerances) from approved company documents, with "
                "citations and a link to the source document. Says so and "
                "points to a human when the policy is not in the knowledge base."
            ),
            tags=["policy", "faq", "procedure", "accounts payable", "procurement"],
            examples=[
                "What's the approval threshold for a purchase order?",
                "How many days do I have to submit an expense report?",
                "What are the steps to onboard a new vendor?",
                "What are our standard invoice payment terms?",
            ],
        ),
    ]


def get_agent_card(base_url: str) -> AgentCard:
    """Build the Agent Card served at ``/.well-known/agent.json``.

    ``base_url`` is the agent's public origin (no trailing slash), e.g.
    ``https://finance-ai-chatbot.cfapps.us10-001.hana.ondemand.com``.
    """
    return AgentCard(
        name=AGENT_NAME,
        description=_DESCRIPTION,
        url=f"{base_url.rstrip('/')}/",
        version=AGENT_VERSION,
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=True),
        skills=_skills(),
    )
