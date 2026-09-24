"""Answer generation.

Turns a user question into an answer string using GPT 5.2 via
:class:`ahf_finance_agent.llm.GenAIHubClient`, applies the output guardrail
scrub, and reports enough metadata for the interaction log.

Build step 3: the model has live, read-only S/4HANA lookups available as tools
(:mod:`ahf_finance_agent.tools`). :meth:`AnswerGenerator.generate` runs a
bounded tool-calling loop — call the model, run any tool calls it makes against
:class:`~ahf_finance_agent.s4hana.S4HANAClient`, feed the results back, repeat
until the model produces a text answer or ``s4hana_max_tool_iterations`` is
hit. There is still no policy knowledge base (step 4), so the system prompt
keeps the model from answering policy questions from general knowledge.

If the model itself is unreachable the generator returns a degraded-but-honest
fallback rather than raising, so a turn is always answered. A *tool* failure is
not fatal: it is handed back to the model as an ``{"error": …}`` result so it
can ask a clarifying question or escalate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ahf_finance_agent.config import get_settings
from ahf_finance_agent.escalation import HUMAN_QUEUE_HINT
from ahf_finance_agent.guardrails import scrub_response
from ahf_finance_agent.llm import GenAIHubClient, LLMError, get_genai_client
from ahf_finance_agent.prompts import RAG_SYSTEM_PROMPT
from ahf_finance_agent.s4hana import S4HANAClient, get_s4hana_client
from ahf_finance_agent.tools import (
    POLICY_TOOL_NAME,
    TOOL_SPECS,
    dispatch_policy_tool,
    dispatch_tool,
    render_tool_content,
)

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
    tools: list[str] = field(default_factory=list)  # tools invoked this turn
    kb_hits: int = 0  # policy passages retrieved by search_policy_docs this turn


class AnswerGenerator:
    def __init__(
        self,
        client: GenAIHubClient | None = None,
        system_prompt: str | None = None,
        *,
        s4_client: S4HANAClient | None = None,
        kb_index=None,
    ) -> None:
        self._client = client
        self._system_prompt = system_prompt or RAG_SYSTEM_PROMPT
        self._s4_client = s4_client
        self._kb_index = kb_index

    @property
    def client(self) -> GenAIHubClient:
        # Lazy: constructing the client resolves a BTP destination, which must
        # not happen at import time or in tests that don't exercise it.
        if self._client is None:
            self._client = get_genai_client()
        return self._client

    @property
    def s4_client(self) -> S4HANAClient:
        # Lazy for the same reason — only touched when the model calls a tool.
        if self._s4_client is None:
            self._s4_client = get_s4hana_client()
        return self._s4_client

    @property
    def kb_index(self):
        # Lazy — only touched when the model calls search_policy_docs. Loading
        # the index reads a file off disk; keep it out of __init__ / tests that
        # don't exercise it.
        if self._kb_index is None:
            from ahf_finance_agent.knowledge_base import get_index

            self._kb_index = get_index()
        return self._kb_index

    def generate(self, question: str, history: list[dict] | None = None) -> Answer:
        """Answer one turn.

        ``history`` is the prior conversation as ``[{"role": "user"|"assistant",
        "content": str}, ...]`` (oldest first, excluding the current question),
        so follow-ups like "is it blocked?" have context. It is trimmed and
        sanitised by :func:`sanitize_history` before use.
        """
        question = (question or "").strip()
        if not question:
            return Answer(
                text="What would you like to know? I can help with AP, procurement, and finance questions.",
                escalated=False,
            )

        messages: list[dict] = [{"role": "system", "content": self._system_prompt}]
        messages.extend(sanitize_history(history))
        messages.append({"role": "user", "content": question})
        max_iterations = get_settings().s4hana_max_tool_iterations
        tools_used: list[str] = []
        grounded = False
        kb_hits = 0

        try:
            result = self.client.chat(messages, tools=TOOL_SPECS)
            for _ in range(max_iterations):
                if not result.tool_calls:
                    break
                messages.append(result.assistant_message or {"role": "assistant", "content": result.text})
                for call in result.tool_calls:
                    tools_used.append(call.name)
                    if call.name == POLICY_TOOL_NAME:
                        outcome = dispatch_policy_tool(call.raw_arguments, self.kb_index)
                        kb_hits += len(outcome.content.get("results", []))
                    else:
                        outcome = dispatch_tool(call.name, call.raw_arguments, self.s4_client)
                    grounded = grounded or outcome.grounded
                    logger.info(
                        "tool call: name=%s grounded=%s", call.name, outcome.grounded
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": render_tool_content(outcome),
                        }
                    )
                result = self.client.chat(messages, tools=TOOL_SPECS)
            else:
                if result.tool_calls:
                    # Iteration budget spent and the model still wants tools —
                    # force one final answer with tools withheld.
                    logger.warning("tool loop hit %s iterations; forcing final answer", max_iterations)
                    messages.append(result.assistant_message or {"role": "assistant", "content": result.text})
                    for call in result.tool_calls:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": '{"error": "lookup budget exhausted for this turn"}',
                            }
                        )
                    result = self.client.chat(messages)
        except LLMError:
            logger.exception("Answer generation failed; returning degraded fallback")
            return Answer(text=_LLM_UNAVAILABLE_FALLBACK, escalated=True, degraded=True)

        if not result.text.strip():
            # Model ended the turn without producing any text (e.g. only tool
            # calls, even after tools were withheld). Degrade to a handoff.
            logger.warning("Answer generation produced no final text; escalating")
            return Answer(
                text=(
                    "I wasn't able to put together an answer for that. "
                    f"Please {HUMAN_QUEUE_HINT}."
                ),
                escalated=True,
                degraded=True,
                tools=list(dict.fromkeys(tools_used)),
                kb_hits=kb_hits,
            )

        safe_text, redactions = scrub_response(result.text)
        if redactions:
            logger.warning("Scrubbed sensitive content from model output: %s", redactions)

        return Answer(
            text=safe_text,
            grounded=grounded,
            escalated=_looks_like_handoff(safe_text),
            redactions=redactions,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            llm_latency_ms=result.latency_ms,
            tools=list(dict.fromkeys(tools_used)),
            kb_hits=kb_hits,
        )


_HISTORY_ROLES = {"user", "assistant"}


def sanitize_history(history: list[dict] | None) -> list[dict]:
    """Normalise caller-supplied conversation history for the model.

    * keeps only ``{"role": "user"|"assistant", "content": <non-empty str>}``
    * drops consecutive duplicates (the A2A task store can double-append)
    * keeps the most recent ``chat_history_max_messages`` entries
    """
    if not history:
        return []
    cleaned: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in _HISTORY_ROLES or not isinstance(content, str) or not content.strip():
            continue
        entry = {"role": role, "content": content.strip()}
        if cleaned and cleaned[-1] == entry:
            continue
        cleaned.append(entry)
    limit = max(0, get_settings().chat_history_max_messages)
    return cleaned[-limit:] if limit else []


def _looks_like_handoff(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in ("finance support team", "finance help channel", "still being set up", "still being loaded")
    )
