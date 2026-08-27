from __future__ import annotations

from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.llm import LLMError
from ahf_finance_agent.prompts import TOOLS_SYSTEM_PROMPT
from tests.helpers import FakeGenAIHubClient, FakeS4HANAClient, text_result, tool_call_result


def test_passes_system_prompt_and_question_and_tools_to_model():
    fake = FakeGenAIHubClient(reply="Here is what I can do...")
    AnswerGenerator(client=fake).generate("what can you do?")
    system, user = fake.calls[0]
    assert system["role"] == "system"
    assert system["content"] == TOOLS_SYSTEM_PROMPT
    assert user == {"role": "user", "content": "what can you do?"}
    # tools are offered on the answering call
    assert fake.tool_specs_seen[0] is not None
    assert any(t["function"]["name"] == "get_invoice_status" for t in fake.tool_specs_seen[0])


def test_scrubs_model_output():
    fake = FakeGenAIHubClient(reply="Contact ap.team@vendor.com for details.")
    answer = AnswerGenerator(client=fake).generate("who do I contact?")
    assert "ap.team@vendor.com" not in answer.text
    assert "email-address" in answer.redactions


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
