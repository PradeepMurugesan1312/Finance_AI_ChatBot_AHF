from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ahf_finance_agent.answering import AnswerGenerator
from ahf_finance_agent.agent_executor import FinanceChatBotExecutor
from ahf_finance_agent.btp import destinations as _destinations
from ahf_finance_agent.config import Settings, get_settings
from ahf_finance_agent.server import build_app
from tests.helpers import FakeGenAIHubClient


@pytest.fixture
def settings() -> Settings:
    # Explicit test config — no .env, no bound services, in-memory task store.
    return Settings(
        app_env="test",
        host="127.0.0.1",
        port=8080,
        public_url="https://finance-ai-chatbot.example.com",
        llm_deployment_id="test-deployment",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    _destinations.clear_cache()
    yield
    get_settings.cache_clear()
    _destinations.clear_cache()


@pytest.fixture
def fake_llm() -> FakeGenAIHubClient:
    return FakeGenAIHubClient()


@pytest.fixture
def client(settings: Settings, fake_llm: FakeGenAIHubClient) -> TestClient:
    executor = FinanceChatBotExecutor(AnswerGenerator(client=fake_llm))
    return TestClient(build_app(settings, executor=executor))


def _send_message(client: TestClient, text: str, *, task_id=None, context_id=None, req_id="1"):
    message: dict = {
        "role": "user",
        "messageId": f"msg-{req_id}",
        "parts": [{"kind": "text", "text": text}],
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "message/send",
            "params": {"message": message},
        },
    )


@pytest.fixture
def send_message():
    """Returns a helper: ``send_message(client, text, task_id=..., context_id=...)``."""
    return _send_message
