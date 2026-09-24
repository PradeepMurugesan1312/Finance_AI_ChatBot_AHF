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


def _message_text(message: Message) -> str:
    parts = []
    for part in message.parts or []:
        root = getattr(part, "root", part)
        text = getattr(root, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def _messages_to_turns(messages, current_input: str) -> list[dict]:
    """a2a ``Message`` list -> ``[{"role": "user"|"assistant", "content": str}]``,
    dropping a trailing entry that just repeats the current question."""
    out: list[dict] = []
    for message in messages or []:
        text = _message_text(message)
        if not text:
            continue
        role = "assistant" if message.role == Role.agent else "user"
        out.append({"role": role, "content": text})
    if out and out[-1]["role"] == "user" and out[-1]["content"] == current_input.strip():
        out.pop()
    return out


def _history_for_generate(task: Task | None, current_input: str) -> list[dict]:
    """Prior conversation from the running task's ``history`` (a2a-sdk keeps
    prior user turns + agent replies there, plus the current message last)."""
    if task is None or not task.history:
        return []
    return _messages_to_turns(task.history, current_input)


def _dedupe_consecutive(turns: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in turns:
        if not out or out[-1] != t:
            out.append(t)
    return out


def _log_inbound_shape(context: RequestContext) -> None:
    """One-off diagnostic: dump every identifier/metadata field on an incoming
    turn so we can see what Joule actually threads across turns. Safe — logs
    keys and ids, not message text (unless LOG_MESSAGE_TEXT)."""
    try:
        import json

        msg = getattr(context, "message", None)
        info: dict = {
            "ctx.task_id": getattr(context, "task_id", None),
            "ctx.context_id": getattr(context, "context_id", None),
            "ctx.metadata": _safe(getattr(context, "metadata", None)),
            "ctx.related_tasks": [getattr(t, "id", None) for t in (getattr(context, "related_tasks", None) or [])],
        }
        if msg is not None:
            info["msg.messageId"] = getattr(msg, "message_id", None)
            info["msg.contextId"] = getattr(msg, "context_id", None)
            info["msg.taskId"] = getattr(msg, "task_id", None)
            info["msg.referenceTaskIds"] = getattr(msg, "reference_task_ids", None)
            info["msg.metadata"] = _safe(getattr(msg, "metadata", None))
            info["msg.extensions"] = getattr(msg, "extensions", None)
        cc = getattr(context, "call_context", None)
        if cc is not None:
            info["call_context.state_keys"] = list(getattr(cc, "state", {}) or {})
            info["call_context.user"] = str(getattr(cc, "user", None))
        logger.info("INBOUND SHAPE %s", json.dumps(info, default=str)[:2000])
    except Exception:  # never let diagnostics break a turn
        logger.warning("could not log inbound shape", exc_info=True)


def _safe(v):
    if v is None:
        return None
    try:
        if hasattr(v, "model_dump"):
            return v.model_dump()
        if isinstance(v, dict):
            return {k: (str(val)[:120]) for k, val in v.items()}
        return str(v)[:300]
    except Exception:
        return "<unserializable>"


class FinanceChatBotExecutor(AgentExecutor):
    """Bridges the finance chatbot agent to the A2A protocol."""

    def __init__(self, answer_generator: AnswerGenerator | None = None, *, task_store=None) -> None:
        # AnswerGenerator's LLM client is lazy — constructing it here does not
        # touch the network or BTP.
        self._answers = answer_generator or AnswerGenerator()
        # Optional: used to rebuild a conversation from earlier tasks that share
        # a contextId, for clients (Joule) that thread only contextId and open a
        # fresh task each turn.
        self._task_store = task_store

    async def _conversation_history(self, context: RequestContext, user_input: str) -> list[dict]:
        """Best-effort prior turns for this conversation.

        Prefers the running task's own history; if the client opened a fresh
        task this turn but kept the same contextId, stitch history from the
        other tasks in that context.
        """
        turns = _history_for_generate(context.current_task, user_input)
        if turns:
            return turns

        store = self._task_store
        ctx_id = context.context_id
        if store is None or not ctx_id or not hasattr(store, "get_by_context"):
            return []
        try:
            prior_tasks = await store.get_by_context(ctx_id)
        except Exception:
            logger.warning("Could not load context history for %s", ctx_id, exc_info=True)
            return []

        current_task_id = context.task_id
        collected: list[dict] = []
        for t in prior_tasks:
            if t.id == current_task_id:
                continue
            for m in t.history or []:
                text = _message_text(m)
                if not text:
                    continue
                role = "assistant" if m.role == Role.agent else "user"
                collected.append({"role": role, "content": text})
            # A task in this repo ends every turn in `input_required` with the
            # answer on status.message — include it if it's not already there.
            sm = getattr(t.status, "message", None)
            if sm is not None:
                text = _message_text(sm)
                if text and (not collected or collected[-1]["content"] != text):
                    collected.append({"role": "assistant", "content": text})
        collected = _dedupe_consecutive(collected)
        if collected and collected[-1]["role"] == "user" and collected[-1]["content"] == user_input.strip():
            collected.pop()
        return collected

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
        _log_inbound_shape(context)

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

        history = await self._conversation_history(context, user_input)
        logger.info(
            "message/send history: task_id=%s context_id=%s prior_messages=%d",
            task.id, task.context_id, len(history),
        )

        question_for_log = user_input if settings.log_message_text else ""
        with track(task.context_id or "", task.id, question_for_log) as rec:
            try:
                # Blocking (openai SDK is sync) — keep it off the event loop.
                answer = await asyncio.to_thread(self._answers.generate, user_input, history)
            except Exception:
                logger.exception("Answer generation raised unexpectedly: task_id=%s", task.id)
                rec.status = "error"
                raise

            rec.status = "answered"
            rec.grounded = answer.grounded
            rec.kb_hits = answer.kb_hits
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
