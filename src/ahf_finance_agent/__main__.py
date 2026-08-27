"""A2A server entry point: ``python -m ahf_finance_agent``.

Cloud Foundry starts this via the ``Procfile`` (``web: python -m
ahf_finance_agent``) and injects ``PORT``.
"""

from __future__ import annotations

import logging

import uvicorn

from ahf_finance_agent.config import get_settings
from ahf_finance_agent.logging_setup import configure_logging
from ahf_finance_agent.server import build_app, resolve_public_url

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

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
