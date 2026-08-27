from __future__ import annotations

import pytest

from ahf_finance_agent.config import Settings


def test_defaults_match_this_tenant():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.aicore_destination_name == "GENAICORE"
    assert s.s4hana_destination_name == "S43"
    assert s.model_name == "gpt-5.2"
    assert s.log_message_text is False  # PII-safe default


def test_require_llm_raises_when_unset():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="LLM_DEPLOYMENT_ID"):
        s.require_llm()


def test_require_llm_returns_deployment_id_when_set():
    s = Settings(llm_deployment_id="dcc9a836b894dc1d", _env_file=None)  # type: ignore[call-arg]
    assert s.require_llm() == "dcc9a836b894dc1d"


def test_is_production_flag():
    assert Settings(app_env="prod", _env_file=None).is_production is True  # type: ignore[call-arg]
    assert Settings(app_env="dev", _env_file=None).is_production is False  # type: ignore[call-arg]


def test_redacted_dict_masks_service_keys():
    s = Settings(destination_service_key='{"clientsecret":"shh"}', _env_file=None)  # type: ignore[call-arg]
    assert s.redacted_dict()["destination_service_key"] == "***set***"
