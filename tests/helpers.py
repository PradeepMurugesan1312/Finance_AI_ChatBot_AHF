"""Shared test doubles."""

from __future__ import annotations

from ahf_finance_agent.llm import ChatResult


class FakeGenAIHubClient:
    """Stand-in for GenAIHubClient — records calls, returns canned text."""

    def __init__(self, reply: str = "Fake model reply.", *, raise_exc: Exception | None = None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, max_completion_tokens=None):
        self.calls.append(list(messages))
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatResult(
            text=self.reply,
            finish_reason="stop",
            model="gpt-5.2-test",
            prompt_tokens=11,
            completion_tokens=7,
            latency_ms=5,
        )
