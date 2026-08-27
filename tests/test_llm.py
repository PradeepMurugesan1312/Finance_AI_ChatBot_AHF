from __future__ import annotations

import httpx
import openai
import pytest

from ahf_finance_agent import llm as llm_mod
from ahf_finance_agent.btp.destinations import ResolvedDestination
from ahf_finance_agent.config import Settings
from ahf_finance_agent.llm import GenAIHubClient, LLMError


def _settings(**over) -> Settings:
    base = dict(app_env="test", llm_deployment_id="dep-123", _env_file=None)
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def _mk_response(text="pong", finish="stop"):
    msg = type("M", (), {"content": text})()
    choice = type("Ch", (), {"message": msg, "finish_reason": finish})()
    usage = type("U", (), {"prompt_tokens": 3, "completion_tokens": 1})()
    return type("R", (), {"choices": [choice], "usage": usage, "model": "gpt-5.2"})()


@pytest.fixture
def patched(monkeypatch):
    capture: dict = {}
    init_kwargs: dict = {}
    state = {"response": _mk_response()}

    def fake_resolve(name, **kw):
        return ResolvedDestination(
            name=name,
            url="https://api.ai.example.com/v2",
            headers={"Authorization": "Bearer tok-abc", "Accept": "application/json"},
            proxy=None,
            authentication="OAuth2ClientCredentials",
            proxy_type="Internet",
        )

    def fake_openai_factory(**kwargs):
        init_kwargs.clear()
        init_kwargs.update(kwargs)
        resp = state["response"]

        class _Completions:
            def create(self, **kw):
                capture.clear()
                capture.update(kw)
                if isinstance(resp, Exception):
                    raise resp
                return resp

        obj = type("FakeOpenAI", (), {})()
        obj.chat = type("Chat", (), {"completions": _Completions()})()
        return obj

    monkeypatch.setattr(llm_mod, "resolve_destination", fake_resolve)
    monkeypatch.setattr(openai, "OpenAI", fake_openai_factory)
    return capture, init_kwargs, state


def test_builds_inference_url_without_doubling_v2(patched):
    _, init_kwargs, _ = patched
    GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert init_kwargs["base_url"] == "https://api.ai.example.com/v2/inference/deployments/dep-123"
    assert init_kwargs["api_key"] == "tok-abc"
    assert init_kwargs["default_query"] == {"api-version": "2023-05-15"}
    assert init_kwargs["default_headers"]["AI-Resource-Group"] == "default"
    assert "Authorization" not in init_kwargs["default_headers"]


def test_sends_max_completion_tokens_not_max_tokens(patched):
    capture, _, _ = patched
    GenAIHubClient(_settings(llm_max_completion_tokens=222)).chat([{"role": "user", "content": "hi"}])
    assert capture["max_completion_tokens"] == 222
    assert "max_tokens" not in capture


def test_returns_text_and_usage(patched):
    result = GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert result.text == "pong"
    assert result.completion_tokens == 1
    assert result.model == "gpt-5.2"


def test_empty_completion_raises_llmerror(patched):
    _, _, state = patched
    state["response"] = _mk_response(text="", finish="length")
    with pytest.raises(LLMError, match="empty completion"):
        GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])


def test_api_status_error_becomes_llmerror(patched):
    _, _, state = patched
    req = httpx.Request("POST", "https://api.ai.example.com/v2/inference/deployments/dep-123/chat/completions")
    resp = httpx.Response(404, request=req, json={"error": "Resource not found"})
    state["response"] = openai.APIStatusError("Resource not found", response=resp, body=None)
    with pytest.raises(LLMError, match="404"):
        GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])


def test_missing_deployment_id_raises_before_network():
    with pytest.raises(Exception, match="LLM_DEPLOYMENT_ID"):
        GenAIHubClient(_settings(llm_deployment_id=None)).chat([{"role": "user", "content": "hi"}])
