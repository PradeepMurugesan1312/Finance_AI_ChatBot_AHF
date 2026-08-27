"""A2A protocol bridge for the AHF Finance ChatBot.

This is the seam between the A2A server (task lifecycle, event queue) and the
agent's actual reasoning.

Build step 3: :meth:`execute` generates the answer with GPT 5.2 via
:class:`ahf_finance_agent.answering.AnswerGenerator`, which now runs read-only
S/4HANA lookups as tools. There is still no policy knowledge base (step 4), so
the system prompt keeps the model from answering policy questions from general
knowledge. If the model is unreachable the generator returns a
degraded-but-honest fallback rather than raising, so a turn is always answered.

Two lifecycle decisions are load-bearing and were proven the hard way by the
sibling agent in this same Joule tenant:

1. **The task never reaches a terminal state.** a2a-sdk rejects any further
   ``message/send`` against a task once it is ``completed`` / ``failed`` /
   ``canceled``, which breaks every Joule follow-up turn in the conversation.
   This is a continuous chatbot, so the task is left ``input_required``.
2. **The answer text goes into ``status.message.parts[0].text``.** Joule's
   dialog function reads exactly that path (plus ``result.body.contextId`` /
   ``result.body.id``). It does not read artifacts. We attach an artifact too
   for other A2A clients / the direct-API demo page.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.config import get_settings
from ahf_finance_agent.observability import track

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class FinanceChatBotExecutor(AgentExecutor):
    """Bridges the finance chatbot agent to the A2A protocol."""

    def __init__(self, answer_generator: AnswerGenerator | None = None) -> None:
        # AnswerGenerator's LLM client is lazy — constructing it here does not
        # touch the network or BTP.
        self._answers = answer_generator or AnswerGenerator()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        settings = get_settings()
        user_input = context.get_user_input() or ""
        task = context.current_task

        logger.info(
            "message/send received: task_id=%s context_id=%s new_task=%s%s",
            context.task_id,
            context.context_id,
            task is None,
            f" input={user_input!r}" if settings.log_message_text else "",
        )

        if task is None:
            task = Task(
                id=context.task_id or str(uuid4()),
                contextId=context.context_id or str(uuid4()),
                status=TaskStatus(state=TaskState.submitted, timestamp=_now()),
            )
            # Deep-copy so this point-in-time "submitted" event isn't mutated by
            # the later status update we enqueue on the same task object.
            await event_queue.enqueue_event(task.model_copy(deep=True))
            logger.info("Created task: task_id=%s context_id=%s", task.id, task.context_id)

        if settings.emit_working_event:
            task.status = TaskStatus(state=TaskState.working, timestamp=_now())
            await event_queue.enqueue_event(task.model_copy(deep=True))

        question_for_log = user_input if settings.log_message_text else ""
        with track(task.context_id or "", task.id, question_for_log) as rec:
            try:
                # Blocking (openai SDK is sync) — keep it off the event loop.
                answer = await asyncio.to_thread(self._answers.generate, user_input)
            except Exception:
                logger.exception("Answer generation raised unexpectedly: task_id=%s", task.id)
                rec.status = "error"
                raise

            rec.status = "answered"
            rec.grounded = answer.grounded
            rec.tools = answer.tools
            rec.escalated = answer.escalated or answer.degraded
            rec.redactions = answer.redactions
            rec.answer_chars = len(answer.text)
            if answer.degraded:
                logger.warning("Task %s answered in degraded mode (LLM unavailable)", task.id)

        parts = [Part(root=TextPart(text=answer.text))]
        agent_msg = Message(messageId=str(uuid4()), role=Role.agent, parts=parts)

        task.status = TaskStatus(
            state=TaskState.input_required,  # non-terminal — see module docstring
            message=agent_msg,
            timestamp=_now(),
        )
        task.history = (task.history or []) + [agent_msg]
        task.artifacts = (task.artifacts or []) + [Artifact(artifactId=str(uuid4()), parts=parts)]

        logger.info("Sending task update: task_id=%s state=%s", task.id, task.status.state)
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is not None:
            logger.info("Canceling task: task_id=%s", task.id)
            task.status = TaskStatus(state=TaskState.canceled, timestamp=_now())
            await event_queue.enqueue_event(task)
