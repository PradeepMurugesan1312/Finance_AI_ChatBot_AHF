"""A2A server entry point: ``python -m ahf_finance_agent``.

Cloud Foundry starts this via the ``Procfile`` (``web: python -m
ahf_finance_agent``) and injects ``PORT``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from ahf_finance_agent.config import Settings, get_settings
from ahf_finance_agent.logging_setup import configure_logging
from ahf_finance_agent.server import build_app, resolve_public_url

logger = logging.getLogger(__name__)


def _ensure_kb_index(settings: Settings) -> None:
    """Build the policy vector index on startup when it is missing or a rebuild
    is forced. Best-effort: a failure here leaves retrieval empty (the agent
    still answers, just without policy grounding) rather than blocking boot.
    """
    if settings.kb_backend.lower() != "local":
        return
    exists = Path(settings.kb_index_path).exists()
    if exists and not settings.kb_rebuild_on_start:
        return
    reason = "forced by KB_REBUILD_ON_START" if exists else f"{settings.kb_index_path} not found"
    logger.info("Building policy knowledge-base index on startup (%s)", reason)
    try:
        from ahf_finance_agent.kb_ingest import build_index

        count = build_index()
        logger.info("Policy KB index ready: %d chunks", count)
    except Exception:
        logger.exception("Policy KB index build failed; continuing without policy grounding")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    _ensure_kb_index(settings)

    logger.info(
        "Starting AHF Finance ChatBot A2A server: bind=%s:%s public_url=%s env=%s",
        settings.host,
        settings.port,
        resolve_public_url(settings),
        settings.app_env,
    )

    app = build_app(settings)
    # Our JSON logging is already configured on the root logger; tell uvicorn
    # not to install its own.
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
