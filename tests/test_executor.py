"""Unit tests for the executor's task lifecycle, without the JSON-RPC layer."""

from __future__ import annotations

import asyncio
import logging

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus, TextPart

from ahf_finance_agent.agent_executor import FinanceChatBotExecutor, _history_for_generate
from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.llm import LLMError
from tests.helpers import FakeGenAIHubClient


def _msg(role: Role, text: str) -> Message:
    return Message(messageId=f"m-{text[:6]}", role=role, parts=[Part(root=TextPart(text=text))])


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


def test_history_for_generate_maps_roles_and_drops_current_question():
    task = Task(
        id="t1", contextId="c1", status=TaskStatus(state=TaskState.input_required),
        history=[
            _msg(Role.user, "what's the status of invoice 5100000017"),
            _msg(Role.agent, "Invoice 5100000017 is posted (status 5)."),
            _msg(Role.user, "is it blocked?"),  # the current turn, appended by a2a-sdk
        ],
    )
    hist = _history_for_generate(task, "is it blocked?")
    assert hist == [
        {"role": "user", "content": "what's the status of invoice 5100000017"},
        {"role": "assistant", "content": "Invoice 5100000017 is posted (status 5)."},
    ]


def test_history_none_when_no_task():
    assert _history_for_generate(None, "hi") == []


class _FakeStore:
    def __init__(self, tasks):
        self._tasks = tasks

    async def get_by_context(self, context_id, limit=20):
        return [t for t in self._tasks if t.context_id == context_id]


def test_context_history_stitched_when_client_opens_fresh_task_each_turn():
    # Prior turn: its own task, answer left on status.message (this repo's shape).
    prior = Task(
        id="task-1", contextId="conv-1",
        status=TaskStatus(state=TaskState.input_required, message=_msg(Role.agent, "It is posted, status 5.")),
        history=[_msg(Role.user, "status of invoice 5100000001?")],
    )
    llm = FakeGenAIHubClient(reply="Here are 10 more.")
    ex = FinanceChatBotExecutor(AnswerGenerator(client=llm), task_store=_FakeStore([prior]))
    # This turn: brand-new task, same contextId (no current_task on the context).
    ctx = _Ctx("give me 10 more", task=None, task_id="task-2", context_id="conv-1")
    asyncio.run(ex.execute(ctx, _Queue()))
    sent = llm.calls[0]
    assert {"role": "user", "content": "status of invoice 5100000001?"} in sent
    assert {"role": "assistant", "content": "It is posted, status 5."} in sent
    assert sent[-1] == {"role": "user", "content": "give me 10 more"}


def test_follow_up_turn_replays_prior_conversation_to_model():
    llm = FakeGenAIHubClient(reply="It is not blocked.")
    ex = FinanceChatBotExecutor(AnswerGenerator(client=llm))
    task = Task(
        id="t1", contextId="c1", status=TaskStatus(state=TaskState.input_required),
        history=[
            _msg(Role.user, "status of invoice 5100000017?"),
            _msg(Role.agent, "It is posted, status 5."),
            _msg(Role.user, "is it blocked?"),
        ],
    )
    q = _Queue()
    asyncio.run(ex.execute(_Ctx("is it blocked?", task=task), q))
    sent = llm.calls[0]
    assert sent[0]["role"] == "system"
    assert {"role": "user", "content": "status of invoice 5100000017?"} in sent
    assert {"role": "assistant", "content": "It is posted, status 5."} in sent
    assert sent[-1] == {"role": "user", "content": "is it blocked?"}


def test_message_text_not_logged_by_default(caplog):
    with caplog.at_level(logging.INFO):
        _run("secret question about invoice 5105601234")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret question" not in joined
