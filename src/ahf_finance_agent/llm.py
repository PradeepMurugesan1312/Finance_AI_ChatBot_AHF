"""GPT 5.2 access through SAP Generative AI Hub (AI Core).

AI Core's GPT deployments expose an OpenAI-compatible inference API. We reach
it through the ``GENAICORE`` BTP destination (:mod:`ahf_finance_agent.btp`),
which resolves to a base URL plus an OAuth2 bearer token, and drive it with the
``openai`` SDK.

Connectivity specifics baked in here (from the sibling build):

* The destination ``URL`` already ends in ``/v2``. The inference path is
  ``{url}/inference/deployments/{deployment_id}`` — do **not** add another
  ``/v2``.
* AI Core routes by deployment id in the path, not by the ``model`` field.
* GPT deployments proxy to Azure OpenAI → every request needs an
  ``api-version`` query param, and the ``AI-Resource-Group`` header.
* GPT 5.2 is reasoning-tier: it rejects ``max_tokens`` — send
  ``max_completion_tokens``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import openai

from ahf_finance_agent.btp import resolve_destination
from ahf_finance_agent.btp.destinations import DestinationError
from ahf_finance_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any failure reaching or getting a usable answer from the model."""


@dataclass
class ToolCall:
    id: str
    name: str
    raw_arguments: str  # JSON string exactly as the model emitted it


@dataclass
class ChatResult:
    text: str
    finish_reason: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The assistant turn to append back to `messages` before the tool results,
    # in the shape the OpenAI chat API expects. None when there are no tool calls.
    assistant_message: dict | None = None


class GenAIHubClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    # -- construction -----------------------------------------------------
    def _build_client(self) -> tuple[openai.OpenAI, str]:
        s = self._settings
        try:
            deployment_id = s.require_llm()
        except RuntimeError as exc:
            # Misconfiguration (no LLM_DEPLOYMENT_ID) must degrade like any other
            # LLM failure — the caller catches LLMError and still answers the
            # turn, rather than the conversation 500-ing.
            raise LLMError(str(exc)) from exc
        try:
            dest = resolve_destination(s.aicore_destination_name)
        except DestinationError as exc:
            raise LLMError(f"Cannot resolve AI Core destination {s.aicore_destination_name!r}: {exc}") from exc

        token = dest.bearer_token
        if not token:
            raise LLMError(
                f"Destination {dest.name!r} did not yield an OAuth2 bearer token "
                f"(Authentication={dest.authentication!r}). AI Core needs an "
                "OAuth2ClientCredentials destination."
            )

        extra_headers = {k: v for k, v in dest.headers.items() if k.lower() not in ("authorization", "accept")}
        extra_headers["AI-Resource-Group"] = s.aicore_resource_group

        client = openai.OpenAI(
            base_url=f"{dest.url}/inference/deployments/{deployment_id}",
            api_key=token,
            default_headers=extra_headers,
            default_query={"api-version": s.aicore_api_version},
            timeout=s.llm_timeout_seconds,
            max_retries=2,
        )
        return client, deployment_id

    # -- inference ------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> ChatResult:
        s = self._settings
        client, deployment_id = self._build_client()
        # NOTE: tool calling needs an AI Core / Azure OpenAI api-version of at
        # least 2023-12-01-preview. Bump AICORE_API_VERSION if a tools request
        # 400s with "Unrecognized request argument: tools".
        kwargs: dict = {
            "model": s.model_name,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens or s.llm_max_completion_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if s.llm_reasoning_effort:
            kwargs["reasoning_effort"] = s.llm_reasoning_effort
        start = time.monotonic()
        try:
            try:
                resp = client.chat.completions.create(**kwargs)
            except openai.APIStatusError as exc:
                # Self-healing, same spirit as s4hana.py's field-drop retries:
                # an older/different AI Core proxy may not recognise
                # reasoning_effort yet. Drop it and retry once rather than
                # failing every single turn over a speed knob.
                if kwargs.get("reasoning_effort") and exc.status_code == 400:
                    logger.warning(
                        "AI Core rejected reasoning_effort=%r (%s); retrying without it",
                        kwargs["reasoning_effort"], exc,
                    )
                    kwargs.pop("reasoning_effort", None)
                    resp = client.chat.completions.create(**kwargs)
                else:
                    raise
        except openai.APIStatusError as exc:
            body = getattr(exc, "message", str(exc))
            logger.error(
                "AI Core inference error: deployment=%s status=%s body=%s",
                deployment_id, exc.status_code, str(body)[:400],
            )
            raise LLMError(f"AI Core returned {exc.status_code}: {str(body)[:200]}") from exc
        except openai.APIError as exc:
            logger.error("AI Core inference failed: deployment=%s err=%s", deployment_id, exc)
            raise LLMError(f"AI Core request failed: {exc}") from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = resp.choices[0] if resp.choices else None
        message = choice.message if choice else None
        text = (message.content if message else "") or ""
        finish = choice.finish_reason if choice else None
        usage = resp.usage

        tool_calls: list[ToolCall] = []
        for raw in (getattr(message, "tool_calls", None) or []):
            fn = getattr(raw, "function", None)
            if fn is None:
                continue
            tool_calls.append(ToolCall(id=raw.id, name=fn.name, raw_arguments=fn.arguments or "{}"))

        if finish == "length":
            logger.warning(
                "AI Core response truncated (finish_reason=length); consider raising "
                "LLM_MAX_COMPLETION_TOKENS (currently %s).",
                max_completion_tokens or s.llm_max_completion_tokens,
            )
        # A turn with tool calls legitimately has no text yet.
        if not text.strip() and not tool_calls:
            raise LLMError(f"AI Core returned an empty completion (finish_reason={finish!r}).")

        assistant_message: dict | None = None
        if tool_calls:
            assistant_message = {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.raw_arguments},
                    }
                    for tc in tool_calls
                ],
            }

        return ChatResult(
            text=text.strip(),
            finish_reason=finish,
            model=getattr(resp, "model", s.model_name),
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_ms=latency_ms,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )


_client: GenAIHubClient | None = None


def get_genai_client() -> GenAIHubClient:
    global _client
    if _client is None:
        _client = GenAIHubClient()
    return _client
