"""Builds the A2A Starlette application.

Kept separate from :mod:`ahf_finance_agent.__main__` so tests can construct the
ASGI app without starting uvicorn.
"""

from __future__ import annotations

import contextlib
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
    # The executor uses the store to reconstruct a conversation from prior tasks
    # in the same context when the client threads only contextId.
    kwargs: dict = {
        "agent_executor": executor or FinanceChatBotExecutor(task_store=task_store),
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
    try:
        from ahf_finance_agent.knowledge_base import embedding_backend_name, get_index

        kb_chunks = get_index().chunk_count()
        kb_embedding_backend = embedding_backend_name()
    except Exception:  # pragma: no cover - defensive; readiness must not 500
        logger.warning("KB status unavailable for /ready", exc_info=True)
        kb_chunks, kb_embedding_backend = 0, "?"
    from ahf_finance_agent.domains import coverage_summary

    cov = coverage_summary()
    return JSONResponse(
        {
            "status": "ready",
            "version": AGENT_VERSION,
            "dependencies": {
                "llm_configured": settings.llm_deployment_id is not None,
                "llm_destination": settings.aicore_destination_name,
                "s4hana_destination": settings.s4hana_destination_name,
                "kb_backend": settings.kb_backend,
                "kb_index_chunks": kb_chunks,
                "kb_embedding_backend": kb_embedding_backend,
                "task_store": "sqlite" if settings.task_store_path else "in-memory",
                "finance_domains": cov["total"],
                "finance_domains_live": len(cov["live"]),
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


async def _diag_s4_samples(request: Request) -> JSONResponse:
    """Non-prod: a handful of real identifiers per lookup, for test setup.

    Keys / statuses only — no names, amounts, or bank data. ``?top=N`` (max 20).
    Disabled when APP_ENV is production.
    """
    import asyncio

    from ahf_finance_agent.s4hana import get_s4hana_client

    settings = get_settings()
    if settings.is_production:
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    try:
        top = int(request.query_params.get("top", "5"))
    except ValueError:
        top = 5
    samples = await asyncio.to_thread(get_s4hana_client().sample_ids, top)
    return JSONResponse({"ok": True, "destination": settings.s4hana_destination_name, "samples": samples})


async def _diag_s4_catalog(_: Request) -> JSONResponse:
    """Probe every S/4HANA capability's candidate services against the live
    tenant and report which one resolved (or none). This is how "is capability
    X connected on this tenant" is verified. Disabled when APP_ENV is production.
    """
    import asyncio

    from ahf_finance_agent.s4hana import get_s4hana_client

    settings = get_settings()
    if settings.is_production:
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    try:
        capabilities = await asyncio.to_thread(get_s4hana_client().probe_catalog)
    except Exception as exc:  # noqa: BLE001 — surface as a clean 502, never 500
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    unresolved = sorted(k for k, v in capabilities.items() if not v["ok"])
    return JSONResponse({
        "ok": not unresolved,
        "destination": settings.s4hana_destination_name,
        "unresolved": unresolved,
        "capabilities": capabilities,
    })


async def _warm_s4_catalog() -> None:
    """Startup hook: probe the S/4HANA capability catalogue once so the first
    real lookup does not pay for it. Best-effort — never blocks or fails boot."""
    import asyncio

    from ahf_finance_agent.s4hana import get_s4hana_client

    try:
        result = await asyncio.to_thread(get_s4hana_client().probe_catalog)
        unresolved = sorted(k for k, v in result.items() if not v["ok"])
        if unresolved:
            logger.warning("S/4HANA catalogue warm-up: unresolved capabilities: %s", unresolved)
        else:
            logger.info("S/4HANA catalogue warm-up: all %d capabilities resolved", len(result))
    except Exception as exc:  # noqa: BLE001 — no destination in local/test is fine
        logger.info("S/4HANA catalogue warm-up skipped: %s", exc)


async def _diag_kb(request: Request) -> JSONResponse:
    """Non-prod smoke test: report the policy index and run one sample retrieval.

    Pass ``?q=...`` to try a specific question. Disabled when APP_ENV is
    production (returns no policy text, but still gated for consistency).
    """
    from ahf_finance_agent.knowledge_base import embedding_backend_name, get_index

    settings = get_settings()
    if settings.is_production:
        return JSONResponse({"error": "disabled in production"}, status_code=403)
    query = request.query_params.get("q", "What is the purchase order approval threshold?")
    try:
        index = get_index()
        hits, grounded = index.retrieve(query)
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse(
        {
            "ok": True,
            "kb_backend": settings.kb_backend,
            "embedding_backend": embedding_backend_name(),
            "index_chunks": index.chunk_count(),
            "query": query,
            "grounded": grounded,
            "top_hits": [
                {"title": h.title, "section": h.section, "source": h.source, "score": round(h.score, 3)}
                for h in hits
            ],
        }
    )


async def _diag_domains(_: Request) -> JSONResponse:
    """The full finance scope and where each domain stands (live lookup vs
    knowledge-base-only). Handy for stakeholders tracking connection readiness.
    """
    from ahf_finance_agent.domains import FINANCE_DOMAINS, coverage_summary

    return JSONResponse(
        {
            "summary": coverage_summary(),
            "domains": [
                {
                    "key": d.key,
                    "name": d.name,
                    "sap_area": d.sap_area,
                    "status": d.status,
                    "odata_services": list(d.odata_services),
                    "kb_docs": list(d.kb_docs),
                    "example_questions": list(d.example_questions),
                }
                for d in FINANCE_DOMAINS
            ],
        }
    )


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
    app.add_route("/diag/s4/samples", _diag_s4_samples, methods=["GET"])
    app.add_route("/diag/s4/catalog", _diag_s4_catalog, methods=["GET"])
    app.add_route("/diag/kb", _diag_kb, methods=["GET"])
    app.add_route("/diag/domains", _diag_domains, methods=["GET"])

    # Probe the S/4HANA capability catalogue once at boot so the first real
    # lookup is fast. Best-effort: _warm_s4_catalog swallows a missing
    # destination (local / test) without failing startup. Starlette 1.x has no
    # add_event_handler, so wrap the built app's lifespan.
    _inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan(app_: Starlette):
        await _warm_s4_catalog()
        async with _inner_lifespan(app_):
            yield

    app.router.lifespan_context = _lifespan
    return app
