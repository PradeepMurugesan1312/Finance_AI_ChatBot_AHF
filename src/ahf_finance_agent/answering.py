"""Answer generation.

Turns a user question into an answer string using GPT 5.2 via
:class:`ahf_finance_agent.llm.GenAIHubClient`, applies the output guardrail
scrub, and reports enough metadata for the interaction log.

Build step 2: no retrieved context and no tools yet — the system prompt
(:data:`ahf_finance_agent.prompts.INTERIM_SYSTEM_PROMPT`) tells the model it is
not grounded on S/4HANA or policy documents, so it should not guess. Steps 3
and 4 add ``context`` and tool results to :meth:`AnswerGenerator.generate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ahf_finance_agent.escalation import HUMAN_QUEUE_HINT
from ahf_finance_agent.guardrails import scrub_response
from ahf_finance_agent.llm import GenAIHubClient, LLMError, get_genai_client
from ahf_finance_agent.prompts import INTERIM_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Used when the model is unreachable / misconfigured. The agent must still
# answer the turn — degraded, honest, and pointing at a human.
_LLM_UNAVAILABLE_FALLBACK = (
    "I can't reach the answering service right now, so I can't respond in "
    f"detail. Please try again shortly, or {HUMAN_QUEUE_HINT}."
)


@dataclass
class Answer:
    text: str
    grounded: bool = False
    escalated: bool = False
    redactions: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    llm_latency_ms: int | None = None
    degraded: bool = False  # True when we fell back because the LLM failed


class AnswerGenerator:
    def __init__(self, client: GenAIHubClient | None = None, system_prompt: str | None = None) -> None:
        self._client = client
        self._system_prompt = system_prompt or INTERIM_SYSTEM_PROMPT

    @property
    def client(self) -> GenAIHubClient:
        # Lazy: constructing the client resolves a BTP destination, which must
        # not happen at import time or in tests that don't exercise it.
        if self._client is None:
            self._client = get_genai_client()
        return self._client

    def generate(self, question: str) -> Answer:
        question = (question or "").strip()
        if not question:
            return Answer(
                text="What would you like to know? I can help with AP, procurement, and finance questions.",
                escalated=False,
            )

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": question},
        ]

        try:
            result = self.client.chat(messages)
        except LLMError:
            logger.exception("Answer generation failed; returning degraded fallback")
            return Answer(text=_LLM_UNAVAILABLE_FALLBACK, escalated=True, degraded=True)

        safe_text, redactions = scrub_response(result.text)
        if redactions:
            logger.warning("Scrubbed sensitive content from model output: %s", redactions)

        return Answer(
            text=safe_text,
            grounded=False,  # step 4 sets this from retrieval
            escalated=_looks_like_handoff(safe_text),
            redactions=redactions,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            llm_latency_ms=result.latency_ms,
        )


def _looks_like_handoff(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in ("finance support team", "finance help channel", "still being set up", "still being loaded")
    )
