from __future__ import annotations

from ahf_finance_agent.tools import dispatch_policy_tool
from tests.helpers import FakeVectorIndex, retrieved


def test_grounded_result_shape_and_citation_guidance():
    idx = FakeVectorIndex(
        hits=[retrieved("PO Approval Thresholds", "Limits",
                        "A purchase order over 100000 requires CFO approval.", score=0.71)],
        grounded=True,
    )
    outcome = dispatch_policy_tool('{"question": "PO approval threshold?"}', idx)

    assert outcome.grounded is True
    assert outcome.content["grounded"] is True
    hit = outcome.content["results"][0]
    assert hit["title"] == "PO Approval Thresholds"
    assert hit["section"] == "Limits"
    assert hit["score"] == 0.71
    assert "ONLY" in outcome.content["message"]
    assert idx.queries == ["PO approval threshold?"]


def test_not_grounded_tells_model_to_hand_off():
    outcome = dispatch_policy_tool('{"question": "what is the capital of France"}', FakeVectorIndex([], False))
    assert outcome.grounded is False
    assert outcome.content["results"] == []
    msg = outcome.content["message"].lower()
    assert "knowledge base" in msg
    assert "general knowledge" in msg


def test_missing_question_is_reported_not_raised():
    outcome = dispatch_policy_tool("{}", FakeVectorIndex([], True))
    assert outcome.grounded is False
    assert "question" in outcome.content["message"]


def test_bad_json_is_reported_not_raised():
    outcome = dispatch_policy_tool("{not json", FakeVectorIndex([], True))
    assert outcome.grounded is False
    assert "JSON" in outcome.content["message"]


def test_index_failure_is_survived():
    idx = FakeVectorIndex(raise_exc=RuntimeError("index unreadable"))
    outcome = dispatch_policy_tool('{"question": "anything"}', idx)
    assert outcome.grounded is False
    assert "Escalate" in outcome.content["message"]


def test_snippet_is_length_capped(monkeypatch):
    from ahf_finance_agent.config import get_settings

    monkeypatch.setenv("KB_SNIPPET_CHARS", "20")
    get_settings.cache_clear()
    idx = FakeVectorIndex(hits=[retrieved("T", "S", "x" * 500)], grounded=True)
    outcome = dispatch_policy_tool('{"question": "q"}', idx)
    assert len(outcome.content["results"][0]["snippet"]) == 20
