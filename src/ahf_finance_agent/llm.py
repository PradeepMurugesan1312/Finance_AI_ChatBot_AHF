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
from dataclasses import dataclass

import openai

from ahf_finance_agent.btp import resolve_destination
from ahf_finance_agent.btp.destinations import DestinationError
from ahf_finance_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Any failure reaching or getting a usable answer from the model."""


@dataclass
class ChatResult:
    text: str
    finish_reason: str | None
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int


class GenAIHubClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    # -- construction -----------------------------------------------------
    def _build_client(self) -> tuple[openai.OpenAI, str]:
        s = self._settings
        deployment_id = s.require_llm()
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
        messages: list[dict[str, str]],
        *,
        max_completion_tokens: int | None = None,
    ) -> ChatResult:
        s = self._settings
        client, deployment_id = self._build_client()
        start = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=s.model_name,
                messages=messages,  # type: ignore[arg-type]
                max_completion_tokens=max_completion_tokens or s.llm_max_completion_tokens,
            )
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
        text = (choice.message.content if choice and choice.message else "") or ""
        finish = choice.finish_reason if choice else None
        usage = resp.usage
        if finish == "length":
            logger.warning(
                "AI Core response truncated (finish_reason=length); consider raising "
                "LLM_MAX_COMPLETION_TOKENS (currently %s).",
                max_completion_tokens or s.llm_max_completion_tokens,
            )
        if not text.strip():
            raise LLMError(f"AI Core returned an empty completion (finish_reason={finish!r}).")

        return ChatResult(
            text=text.strip(),
            finish_reason=finish,
            model=getattr(resp, "model", s.model_name),
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_ms=latency_ms,
        )


_client: GenAIHubClient | None = None


def get_genai_client() -> GenAIHubClient:
    global _client
    if _client is None:
        _client = GenAIHubClient()
    return _client
