from __future__ import annotations

from ahf_finance_agent.answering import AnswerGenerator, sanitize_history
from ahf_finance_agent.llm import LLMError
from ahf_finance_agent.prompts import RAG_SYSTEM_PROMPT
from tests.helpers import (
    FakeGenAIHubClient,
    FakeS4HANAClient,
    FakeVectorIndex,
    retrieved,
    text_result,
    tool_call_result,
)


def test_passes_system_prompt_and_question_and_tools_to_model():
    fake = FakeGenAIHubClient(reply="Here is what I can do...")
    AnswerGenerator(client=fake).generate("what can you do?")
    system, user = fake.calls[0]
    assert system["role"] == "system"
    assert system["content"] == RAG_SYSTEM_PROMPT
    assert user == {"role": "user", "content": "what can you do?"}
    # tools are offered on the answering call — S/4HANA lookups + the policy KB
    assert fake.tool_specs_seen[0] is not None
    offered = {t["function"]["name"] for t in fake.tool_specs_seen[0]}
    assert "get_invoice_status" in offered
    assert "search_policy_docs" in offered
    # procure-to-pay widening: goods receipts, PO approval, invoice items
    assert {"get_goods_receipts_for_po", "get_purchase_order_approval_status", "get_invoice_items"} <= offered


def test_scrubs_model_output():
    fake = FakeGenAIHubClient(reply="The SSN on file is 123-45-6789.")
    answer = AnswerGenerator(client=fake).generate("who owns this invoice?")
    assert "123-45-6789" not in answer.text
    assert "ssn-or-tax-id" in answer.redactions


def test_does_not_scrub_email_addresses_or_bank_details():
    # Deliberate, narrow exceptions (2026-09): get_vendor_email_addresses and
    # get_vendor_bank_accounts are allowed to surface those fields, so the
    # text-level scrub no longer blanket-redacts emails or IBAN/bank-account
    # patterns (field-level SENSITIVE_KEYS still blocks both on every other
    # lookup).
    fake = FakeGenAIHubClient(
        reply="The contact email is ap.team@vendor.com and the IBAN on file is DE89370400440532013000."
    )
    answer = AnswerGenerator(client=fake).generate("what's the vendor's contact email?")
    assert "ap.team@vendor.com" in answer.text
    assert "DE89370400440532013000" in answer.text
    assert "email-address" not in answer.redactions
    assert "iban" not in answer.redactions


def test_llm_failure_returns_degraded_escalated_answer():
    fake = FakeGenAIHubClient(raise_exc=LLMError("AI Core returned 404"))
    answer = AnswerGenerator(client=fake).generate("what is invoice 5105601234 status?")
    assert answer.degraded is True
    assert answer.escalated is True
    assert "can't reach the answering service" in answer.text


def test_empty_question_short_circuits_without_calling_model():
    fake = FakeGenAIHubClient()
    answer = AnswerGenerator(client=fake).generate("   ")
    assert fake.calls == []
    assert answer.escalated is False


def test_handoff_phrasing_flags_escalation():
    fake = FakeGenAIHubClient(reply="The policy knowledge base is still being loaded; contact the finance support team.")
    answer = AnswerGenerator(client=fake).generate("what's the PO approval threshold?")
    assert answer.escalated is True
    assert answer.grounded is False


# --- tool loop -------------------------------------------------------

def test_runs_tool_then_answers_from_result_and_marks_grounded():
    s4 = FakeS4HANAClient(
        get_invoice_status={"SupplierInvoice": "5105601234", "SupplierInvoiceStatus": "5", "PaymentBlockingReason": ""}
    )
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("get_invoice_status", '{"invoice": "5105601234", "fiscal_year": "2026"}'),
            text_result("Invoice 5105601234 is posted and not blocked for payment."),
        ]
    )
    answer = AnswerGenerator(client=fake, s4_client=s4).generate("is invoice 5105601234 (FY2026) paid?")

    assert s4.calls[0][0] == "get_invoice_status"
    assert answer.tools == ["get_invoice_status"]
    assert answer.grounded is True
    assert answer.escalated is False
    assert "5105601234" in answer.text
    # model saw: answering call, then a follow-up call carrying the tool result
    assert len(fake.calls) == 2
    assert fake.calls[1][-1]["role"] == "tool"


def test_runs_goods_receipt_lookup_and_marks_grounded():
    s4 = FakeS4HANAClient(
        get_goods_receipts_for_po={
            "purchaseOrder": "4500001234",
            "goodsReceiptItemCount": 2,
            "hasActiveGoodsReceipt": True,
            "items": [{"MaterialDocument": "5000000001", "GoodsMovementType": "101"}],
        }
    )
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("get_goods_receipts_for_po", '{"purchase_order": "4500001234"}'),
            text_result("PO 4500001234 has 2 goods receipt lines; the delivery was booked."),
        ]
    )
    answer = AnswerGenerator(client=fake, s4_client=s4).generate("has PO 4500001234 been received?")
    assert s4.calls[0][0] == "get_goods_receipts_for_po"
    assert answer.tools == ["get_goods_receipts_for_po"]
    assert answer.grounded is True


def test_tool_miss_is_not_grounded_and_not_fatal():
    s4 = FakeS4HANAClient(get_purchase_order_status=None)
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("get_purchase_order_status", '{"purchase_order": "4500009999"}'),
            text_result("I couldn't find purchase order 4500009999 in S/4HANA."),
        ]
    )
    answer = AnswerGenerator(client=fake, s4_client=s4).generate("status of PO 4500009999?")
    assert answer.tools == ["get_purchase_order_status"]
    assert answer.grounded is False
    assert "4500009999" in answer.text


def test_s4hana_error_is_handed_back_to_model_not_raised():
    from ahf_finance_agent.s4hana import S4HANAError

    s4 = FakeS4HANAClient(get_vendor_details=S4HANAError("S/4HANA returned 503: service unavailable"))
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("get_vendor_details", '{"business_partner": "100000"}'),
            text_result("I can't reach vendor master data right now — contact the finance support team."),
        ]
    )
    answer = AnswerGenerator(client=fake, s4_client=s4).generate("is vendor 100000 set up?")
    assert answer.grounded is False
    assert answer.escalated is True  # "finance support team" phrasing
    tool_msg = fake.calls[1][-1]
    assert tool_msg["role"] == "tool"
    assert "error" in tool_msg["content"]


# --- multi-turn conversation --------------------------------------

def test_history_is_replayed_between_system_and_current_question():
    fake = FakeGenAIHubClient(reply="No, it is not blocked.")
    history = [
        {"role": "user", "content": "status of invoice 5100000017?"},
        {"role": "assistant", "content": "It is posted (status 5)."},
    ]
    AnswerGenerator(client=fake).generate("is it blocked?", history)
    sent = fake.calls[0]
    assert sent[0]["role"] == "system"
    assert sent[1:3] == history
    assert sent[-1] == {"role": "user", "content": "is it blocked?"}


def test_sanitize_history_filters_dedupes_and_caps(monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_MAX_MESSAGES", "3")
    from ahf_finance_agent.config import get_settings

    get_settings.cache_clear()
    raw = [
        {"role": "system", "content": "nope"},          # wrong role → dropped
        {"role": "user", "content": "  "},               # blank → dropped
        {"role": "user", "content": "a"},
        {"role": "user", "content": "a"},                # consecutive dup → collapsed
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    assert sanitize_history(raw) == [
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    assert sanitize_history(None) == []


# --- policy knowledge base (RAG) ------------------------------------

def test_policy_tool_grounded_answer_is_marked_grounded_with_kb_hits():
    kb = FakeVectorIndex(
        hits=[retrieved("PO Approval Thresholds", "Single purchase order approval limits",
                        "A purchase order over 100000 requires CFO approval.")],
        grounded=True,
    )
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("search_policy_docs", '{"question": "what is the PO approval threshold?"}'),
            text_result("Anything over USD 100,000 needs CFO approval "
                        "(PO Approval Thresholds — Single purchase order approval limits)."),
        ]
    )
    answer = AnswerGenerator(client=fake, kb_index=kb).generate("what is the PO approval threshold?")

    assert answer.tools == ["search_policy_docs"]
    assert answer.grounded is True
    assert answer.kb_hits == 1
    assert answer.escalated is False
    assert kb.queries == ["what is the PO approval threshold?"]
    assert fake.calls[1][-1]["role"] == "tool"


def test_policy_tool_not_grounded_leads_to_handoff():
    kb = FakeVectorIndex(hits=[], grounded=False)
    fake = FakeGenAIHubClient(
        script=[
            tool_call_result("search_policy_docs", '{"question": "what is the parental leave policy?"}'),
            text_result("That policy isn't in the knowledge base yet — please contact "
                        "the AHF finance support team."),
        ]
    )
    answer = AnswerGenerator(client=fake, kb_index=kb).generate("what is the parental leave policy?")

    assert answer.grounded is False
    assert answer.kb_hits == 0
    assert answer.escalated is True  # "finance support team" phrasing


def test_tool_loop_stops_at_iteration_budget():
    s4 = FakeS4HANAClient(get_invoice_status={"SupplierInvoice": "1"})
    # Model keeps asking for tools; loop must terminate and force a final answer.
    # initial call + 4 loop iterations = 5 tool-call turns, then the forced final.
    script = [tool_call_result("get_invoice_status", '{"invoice": "1", "fiscal_year": "2026"}') for _ in range(5)]
    script.append(text_result("stopping"))
    fake = FakeGenAIHubClient(script=script)
    answer = AnswerGenerator(client=fake, s4_client=s4).generate("loop please")
    assert answer.text == "stopping"
    assert len(fake.calls) == 6
    assert fake.tool_specs_seen[-1] is None  # tools withheld on the forced final call
