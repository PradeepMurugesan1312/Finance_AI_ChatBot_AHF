"""One structured record per answered turn, for pilot measurement.

The design brief wants AI Core inference observability on "from day one" and a
way to measure grounding-failure and escalation rate. Free-text app logs can't
answer "what % of turns escalated last week", so every turn emits one JSON
record on the ``ahf_agent.interactions`` logger (and, if
``INTERACTION_LOG_PATH`` is set, appends it to that file for offline analysis).

No PII is added here. Question text is included only when
``LOG_MESSAGE_TEXT=true`` (checked by the caller); the answer is scrubbed by
the response guardrail before it reaches this module.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator

logger = logging.getLogger("ahf_agent.interactions")


@dataclass
class InteractionRecord:
    context_id: str = ""
    task_id: str = ""
    question: str = ""
    grounded: bool = False
    kb_hits: int = 0
    tools: list[str] = field(default_factory=list)
    escalated: bool = False
    out_of_scope: bool = False
    redactions: list[str] = field(default_factory=list)
    status: str = ""
    answer_chars: int = 0
    latency_ms: int = 0
    ts: float = 0.0

    def emit(self) -> None:
        self.ts = time.time()
        try:
            line = json.dumps(asdict(self), ensure_ascii=False, default=str)
        except Exception:  # never let logging break a response
            logger.exception("Failed to serialize interaction record")
            return
        logger.info("interaction %s", line)
        path = os.getenv("INTERACTION_LOG_PATH")
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                logger.warning("Could not append interaction to %s", path, exc_info=True)


@contextmanager
def track(context_id: str = "", task_id: str = "", question: str = "") -> Iterator[InteractionRecord]:
    """Time an interaction and emit its record on exit, even on exception."""
    rec = InteractionRecord(context_id=context_id, task_id=task_id, question=question)
    start = time.monotonic()
    try:
        yield rec
    finally:
        rec.latency_ms = int((time.monotonic() - start) * 1000)
        rec.emit()
