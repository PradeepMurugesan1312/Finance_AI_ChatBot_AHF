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
    assert init_kwargs["default_query"] == {"api-version": "2024-10-21"}
    assert init_kwargs["default_headers"]["AI-Resource-Group"] == "default"
    assert "Authorization" not in init_kwargs["default_headers"]


def test_sends_max_completion_tokens_not_max_tokens(patched):
    capture, _, _ = patched
    GenAIHubClient(_settings(llm_max_completion_tokens=222)).chat([{"role": "user", "content": "hi"}])
    assert capture["max_completion_tokens"] == 222
    assert "max_tokens" not in capture


def test_forwards_tools_and_defaults_tool_choice_to_auto(patched):
    capture, _, _ = patched
    tools = [{"type": "function", "function": {"name": "get_invoice_status", "parameters": {}}}]
    GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}], tools=tools)
    assert capture["tools"] == tools
    assert capture["tool_choice"] == "auto"


def test_no_tools_key_when_tools_not_given(patched):
    capture, _, _ = patched
    GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert "tools" not in capture and "tool_choice" not in capture


def test_parses_tool_calls_and_builds_assistant_message(patched):
    _, _, state = patched
    fn = type("F", (), {"name": "get_invoice_status", "arguments": '{"invoice": "1"}'})()
    tc = type("TC", (), {"id": "call-1", "function": fn})()
    msg = type("M", (), {"content": None, "tool_calls": [tc]})()
    choice = type("Ch", (), {"message": msg, "finish_reason": "tool_calls"})()
    state["response"] = type("R", (), {"choices": [choice], "usage": None, "model": "gpt-5.2"})()

    result = GenAIHubClient(_settings()).chat([{"role": "user", "content": "status of invoice 1?"}])
    assert result.text == ""  # no LLMError despite empty content — there are tool calls
    assert [(c.name, c.raw_arguments) for c in result.tool_calls] == [("get_invoice_status", '{"invoice": "1"}')]
    assert result.assistant_message["tool_calls"][0]["function"]["name"] == "get_invoice_status"
    assert result.assistant_message["role"] == "assistant"


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


def test_sends_reasoning_effort_by_default(patched):
    capture, _, _ = patched
    GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert capture["reasoning_effort"] == "high"


def test_reasoning_effort_can_be_disabled(patched):
    capture, _, _ = patched
    GenAIHubClient(_settings(llm_reasoning_effort=None)).chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in capture


def test_reasoning_effort_falls_back_when_ai_core_rejects_it(monkeypatch):
    calls: list[dict] = []

    def fake_resolve(name, **kw):
        return ResolvedDestination(
            name=name,
            url="https://api.ai.example.com/v2",
            headers={"Authorization": "Bearer tok-abc", "Accept": "application/json"},
            proxy=None,
            authentication="OAuth2ClientCredentials",
            proxy_type="Internet",
        )

    req = httpx.Request("POST", "https://api.ai.example.com/v2/inference/deployments/dep-123/chat/completions")
    resp_400 = httpx.Response(400, request=req, json={"error": "Unrecognized request argument: reasoning_effort"})

    def fake_openai_factory(**kwargs):
        class _Completions:
            def create(self, **kw):
                calls.append(kw)
                if "reasoning_effort" in kw:
                    raise openai.APIStatusError("bad request", response=resp_400, body=None)
                return _mk_response()

        obj = type("FakeOpenAI", (), {})()
        obj.chat = type("Chat", (), {"completions": _Completions()})()
        return obj

    monkeypatch.setattr(llm_mod, "resolve_destination", fake_resolve)
    monkeypatch.setattr(openai, "OpenAI", fake_openai_factory)

    result = GenAIHubClient(_settings()).chat([{"role": "user", "content": "hi"}])
    assert result.text == "pong"
    assert len(calls) == 2
    assert calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in calls[1]
