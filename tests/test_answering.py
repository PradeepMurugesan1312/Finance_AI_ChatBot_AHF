from __future__ import annotations

from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.llm import LLMError
from ahf_finance_agent.prompts import INTERIM_SYSTEM_PROMPT
from tests.helpers import FakeGenAIHubClient


def test_passes_system_prompt_and_question_to_model():
    fake = FakeGenAIHubClient(reply="Here is what I can do...")
    AnswerGenerator(client=fake).generate("what can you do?")
    system, user = fake.calls[0]
    assert system["role"] == "system"
    assert system["content"] == INTERIM_SYSTEM_PROMPT
    assert user == {"role": "user", "content": "what can you do?"}


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
    fake = FakeGenAIHubClient(reply="Live S/4HANA lookups are still being set up; contact the finance support team.")
    answer = AnswerGenerator(client=fake).generate("status of PO 4500001234?")
    assert answer.escalated is True
    assert answer.grounded is False
