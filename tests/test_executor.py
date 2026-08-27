"""Unit tests for the executor's task lifecycle, without the JSON-RPC layer."""

from __future__ import annotations

import asyncio
import logging

from a2a.types import Task, TaskState, TaskStatus

from ahf_finance_agent.agent_executor import FinanceChatBotExecutor
from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.llm import LLMError
from tests.helpers import FakeGenAIHubClient


class _Ctx:
    """Minimal stand-in for a2a's RequestContext."""

    def __init__(self, text: str, task: Task | None = None, task_id=None, context_id=None):
        self._text = text
        self.current_task = task
        self.task_id = task_id
        self.context_id = context_id

    def get_user_input(self) -> str:
        return self._text


class _Queue:
    def __init__(self):
        self.events: list = []

    async def enqueue_event(self, event):
        self.events.append(event)


def _executor(reply="Fake model reply.", raise_exc=None):
    llm = FakeGenAIHubClient(reply=reply, raise_exc=raise_exc)
    return FinanceChatBotExecutor(AnswerGenerator(client=llm))


def _run(text: str, executor=None, **ctx_kwargs):
    ex = executor or _executor()
    q = _Queue()
    asyncio.run(ex.execute(_Ctx(text, **ctx_kwargs), q))
    return q.events


def test_new_conversation_emits_submitted_then_input_required():
    events = _run("hello")
    assert [e.status.state for e in events] == [TaskState.submitted, TaskState.input_required]
    assert events[-1].status.message.parts[0].root.text == "Fake model reply."


def test_generates_ids_when_context_omits_them():
    task = _run("hello")[-1]
    assert task.id
    assert task.context_id


def test_cancel_sets_canceled_state():
    ex = _executor()
    q = _Queue()
    task = Task(id="t1", contextId="c1", status=TaskStatus(state=TaskState.working))
    asyncio.run(ex.cancel(_Ctx("", task=task), q))
    assert q.events[-1].status.state == TaskState.canceled


def test_llm_failure_degrades_but_still_answers_and_flags_escalation(caplog):
    with caplog.at_level(logging.INFO, logger="ahf_agent.interactions"):
        events = _run("hello", executor=_executor(raise_exc=LLMError("boom")))
    task = events[-1]
    assert task.status.state == TaskState.input_required
    assert "can't reach the answering service" in task.status.message.parts[0].root.text
    line = next(r.message for r in caplog.records if r.name == "ahf_agent.interactions")
    assert '"escalated": true' in line


def test_successful_answer_is_not_flagged_as_escalated(caplog):
    with caplog.at_level(logging.INFO, logger="ahf_agent.interactions"):
        _run("what can you do?")
    line = next(r.message for r in caplog.records if r.name == "ahf_agent.interactions")
    assert '"escalated": false' in line
    assert '"grounded": false' in line


def test_message_text_not_logged_by_default(caplog):
    with caplog.at_level(logging.INFO):
        _run("secret question about invoice 5105601234")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret question" not in joined
