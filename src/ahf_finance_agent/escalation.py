"""Human-escalation messaging.

The confidence bar and routing logic land in build step 6. For now this module
holds the single source of truth for *what the user is told* when the agent
hands off, so the wording is consistent everywhere it is used (interim
scaffold response, out-of-scope deflection, low-confidence RAG answer).
"""

from __future__ import annotations

# TODO(step 6): replace with the real queue address / ticket link for AHF.
HUMAN_QUEUE_HINT = (
    "you can reach the AHF finance support team through the usual finance "
    "help channel for a definitive answer"
)


def escalation_sentence(reason: str | None = None) -> str:
    lead = "I'm not able to answer that confidently."
    if reason:
        lead = f"{lead} {reason[0].upper()}{reason[1:]}."
    return f"{lead} For anything time-sensitive, {HUMAN_QUEUE_HINT}."
