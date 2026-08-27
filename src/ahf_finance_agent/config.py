"""Runtime configuration, loaded from environment variables.

Everything the agent needs to run is read here once at startup so the rest of
the code never touches ``os.environ`` directly and misconfiguration fails fast
with a clear message instead of a ``KeyError`` deep in a request.

Step 1 (this scaffold) needs almost none of these — the AI Core / S/4HANA
fields are declared now, validated as a group by :func:`Settings.require_llm`
and friends, and wired up in later build steps.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Server -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    # "dev" relaxes a few guards (e.g. allows NoAuthentication path, verbose
    # errors). Anything else is treated as production.
    app_env: str = Field(default="dev")
    log_level: str = "INFO"
    # Off by default: raw user message text is treated as potentially
    # sensitive and is never written to logs unless this is explicitly set.
    log_message_text: bool = False

    # --- Public URL override ----------------------------------------------
    # Normally derived from VCAP_APPLICATION.application_uris on Cloud Foundry;
    # set this for local runs behind a tunnel, or to pin the agent-card URL.
    public_url: str | None = None

    # --- SAP Generative AI Hub / AI Core (step 2) ------------------------
    model_name: str = "gpt-5.2"  # label only; AI Core routes by deployment id
    llm_deployment_id: str | None = None
    embedding_deployment_id: str | None = None
    embedding_model_name: str = "text-embedding-3-small"
    aicore_resource_group: str = "default"
    aicore_destination_name: str = "GENAICORE"
    # AI Core GPT deployments proxy to Azure OpenAI, which requires an
    # api-version query param on every request. Must be >= 2023-12-01-preview
    # for tool/function calling (see llm.py) — older values 400 with
    # "Unrecognized request argument: tools".
    aicore_api_version: str = "2024-10-21"
    # GPT 5.2 is a reasoning-tier model: it rejects the legacy `max_tokens`
    # chat param — the openai client sends `max_completion_tokens`.
    llm_max_completion_tokens: int = 4096
    # Kept well under the 60s synchronous A2A budget (step 5 adds the async
    # webhook path for anything slower).
    llm_timeout_seconds: float = 45.0

    # --- S/4HANA connectivity (step 3) ----------------------------------
    s4hana_destination_name: str = "S43"
    # OData service root, prepended to every service path. Keeps the S43
    # destination URL as the bare host:port (e.g. http://host:50000) — the
    # standard SAP Gateway root is /sap/opu/odata/sap. Set
    # S4HANA_ODATA_BASE_PATH="" if the destination URL already carries it.
    s4hana_odata_base_path: str = "/sap/opu/odata/sap"
    s4hana_timeout_seconds: float = 20.0
    # Max GPT 5.2 <-> S/4HANA tool round trips before we force a final answer.
    # 4 covers "look up A, then look up B it referenced" without runaway loops.
    s4hana_max_tool_iterations: int = 4

    # --- Local dev credentials (never set in CF; services are bound there) --
    destination_service_key: str | None = None
    connectivity_service_key: str | None = None

    # --- RAG knowledge base (step 4) ----------------------------------
    kb_backend: str = "local"  # "local" | "hana"
    kb_index_path: str = "knowledge_base/index.json"
    kb_docs_dir: str = "knowledge_base/docs"
    kb_min_score: float = 0.20
    kb_top_k: int = 4

    # --- A2A task persistence (step 5 / step 7) ----------------------
    task_store_path: str | None = None
    emit_working_event: bool = False

    # --- Observability (step 9) --------------------------------------
    interaction_log_path: str | None = None
    feedback_log_path: str | None = None

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env.lower() not in ("dev", "development", "local", "test")

    def require_llm(self) -> str:
        """Return the AI Core deployment id or raise if it is not configured.

        Called by the answer-generation path (step 2), not at import time, so
        the scaffold and its tests run without AI Core credentials.
        """
        if not self.llm_deployment_id:
            raise RuntimeError(
                "LLM_DEPLOYMENT_ID is not set — cannot reach the GPT 5.2 "
                "deployment on SAP AI Core. Set it in the environment "
                "(manifest.yml `env:` on Cloud Foundry, or .env locally)."
            )
        return self.llm_deployment_id

    def redacted_dict(self) -> dict[str, Any]:
        """Config snapshot safe to log at startup (secrets masked)."""
        data = self.model_dump()
        for key in ("destination_service_key", "connectivity_service_key"):
            if data.get(key):
                data[key] = "***set***"
        return data


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:  # pragma: no cover - startup guard
        raise RuntimeError(f"Invalid configuration:\n{exc}") from exc
    logger.info("Configuration loaded: %s", json.dumps(settings.redacted_dict(), default=str))
    return settings
