"""Builds the A2A Starlette application.

Kept separate from :mod:`ahf_finance_agent.__main__` so tests can construct the
ASGI app without starting uvicorn.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps.jsonrpc.starlette_app import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from ahf_finance_agent.agent_card import AGENT_VERSION, get_agent_card
from ahf_finance_agent.agent_executor import FinanceChatBotExecutor
from ahf_finance_agent.config import Settings, get_settings
from ahf_finance_agent.task_store import build_task_store

logger = logging.getLogger(__name__)


def resolve_public_url(settings: Settings) -> str:
    """The agent's public origin, used to render the agent-card ``url``.

    Cloud Foundry injects ``VCAP_APPLICATION`` with the bound route(s); locally
    we fall back to host:port unless ``PUBLIC_URL`` is set explicitly.
    """
    if settings.public_url:
        return settings.public_url.rstrip("/")
    try:
        uris = json.loads(os.getenv("VCAP_APPLICATION", "{}")).get("application_uris", [])
    except (ValueError, TypeError):
        uris = []
    if uris:
        return f"https://{uris[0]}"
    return f"http://{settings.host}:{settings.port}"


def _build_request_handler(executor: AgentExecutor | None = None) -> DefaultRequestHandler:
    task_store = build_task_store()
    kwargs: dict = {
        "agent_executor": executor or FinanceChatBotExecutor(),
        "task_store": task_store,
    }
    try:
        from a2a.server.tasks import (
            BasePushNotificationSender,
            InMemoryPushNotificationConfigStore,
        )

        config_store = InMemoryPushNotificationConfigStore()
        kwargs["push_config_store"] = config_store
        kwargs["push_sender"] = BasePushNotificationSender(
            httpx_client=httpx.AsyncClient(timeout=30),
            config_store=config_store,
        )
        logger.info("Push notifications (async webhook) enabled")
    except Exception:  # pragma: no cover - depends on a2a-sdk internals
        logger.warning("Push notification components unavailable; synchronous only", exc_info=True)
    return DefaultRequestHandler(**kwargs)


async def _health(_: Request) -> JSONResponse:
    """Liveness probe — process is up and serving."""
    return JSONResponse({"status": "ok", "version": AGENT_VERSION})


async def _ready(_: Request) -> JSONResponse:
    """Readiness probe — reports which downstream wiring is configured.

    Nothing downstream is *required* for the process to serve (a failed LLM
    degrades gracefully), so this stays ``ready``; the flags show what is wired
    as build steps land.
    """
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ready",
            "version": AGENT_VERSION,
            "dependencies": {
                "llm_configured": settings.llm_deployment_id is not None,
                "llm_destination": settings.aicore_destination_name,
                "s4hana_destination": settings.s4hana_destination_name,
                "kb_backend": settings.kb_backend,
                "task_store": "sqlite" if settings.task_store_path else "in-memory",
            },
        }
    )


async def _diag_llm(_: Request) -> JSONResponse:
    """Non-prod smoke test: resolve GENAICORE and do a one-word chat call.

    Costs a handful of tokens — disabled when APP_ENV is production.
    """
    import asyncio

    from ahf_finance_agent.llm import LLMError, get_genai_client

    settings = get_settings()
    if settings.is_production:
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    try:
        result = await asyncio.to_thread(
            get_genai_client().chat,
            [{"role": "user", "content": "Reply with the single word: pong"}],
            max_completion_tokens=1000,
        )
    except LLMError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse(
        {
            "ok": True,
            "model": result.model,
            "text": result.text[:200],
            "finish_reason": result.finish_reason,
            "latency_ms": result.latency_ms,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        }
    )


async def _diag_s4(_: Request) -> JSONResponse:
    """Non-prod smoke test: resolve S43 and do a one-row OData GET.

    Proves the destination resolves and the agent's credentials are accepted by
    S/4HANA, without returning any business data. Disabled when APP_ENV is
    production.
    """
    import asyncio

    from ahf_finance_agent.s4hana import S4HANAError, get_s4hana_client

    settings = get_settings()
    if settings.is_production:
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    try:
        result = await asyncio.to_thread(get_s4hana_client().ping)
    except S4HANAError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "destination": settings.s4hana_destination_name, **result})


def build_app(settings: Settings | None = None, *, executor: AgentExecutor | None = None) -> Starlette:
    settings = settings or get_settings()
    public_url = resolve_public_url(settings)
    logger.info("Agent card URL: %s", public_url)

    a2a_app = A2AStarletteApplication(
        agent_card=get_agent_card(public_url),
        http_handler=_build_request_handler(executor),
    )
    app: Starlette = a2a_app.build()
    app.add_route("/health", _health, methods=["GET"])
    app.add_route("/ready", _ready, methods=["GET"])
    app.add_route("/diag/llm", _diag_llm, methods=["GET"])
    app.add_route("/diag/s4", _diag_s4, methods=["GET"])
    return app
