from __future__ import annotations

import json

import httpx
import pytest

from ahf_finance_agent.btp import destinations as d

DS_KEY = json.dumps(
    {"uri": "https://dest.example.com", "clientid": "ds-id", "clientsecret": "ds-secret", "url": "https://uaa.example.com"}
)


class _FakeHTTP:
    """Routes httpx.get/post by URL to canned responses."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.routes: dict[str, dict] = {}

    def route(self, needle: str, json_body: dict, status: int = 200):
        self.routes[needle] = {"json": json_body, "status": status}

    def _respond(self, method: str, url: str):
        self.requests.append((method, url))
        for needle, spec in self.routes.items():
            if needle in url:
                return httpx.Response(spec["status"], json=spec["json"], request=httpx.Request(method, url))
        return httpx.Response(404, json={"error": "no route"}, request=httpx.Request(method, url))

    def get(self, url, **kw):
        return self._respond("GET", url)

    def post(self, url, **kw):
        return self._respond("POST", url)


@pytest.fixture
def fake_http(monkeypatch):
    fake = _FakeHTTP()
    fake.route("//uaa.example.com/oauth/token", {"access_token": "ds-token", "expires_in": 3600})
    monkeypatch.setattr(d, "httpx", fake)
    monkeypatch.setenv("DESTINATION_SERVICE_KEY", DS_KEY)
    return fake


def _dest_payload(config: dict, auth_tokens: list | None = None) -> dict:
    return {"destinationConfiguration": config, "authTokens": auth_tokens or []}


def test_no_auth_destination_resolves_url_without_trailing_slash(fake_http):
    fake_http.route(
        "/destinations/S43",
        _dest_payload({"URL": "https://s4.example.com/", "Authentication": "NoAuthentication", "ProxyType": "Internet"}),
    )
    resolved = d.resolve_destination("S43")
    assert resolved.url == "https://s4.example.com"
    assert resolved.bearer_token is None


def test_oauth_client_credentials_falls_back_to_manual_exchange_on_csrf_bug(fake_http):
    # authTokens present but carrying an error (the known CSRF auto-fetch bug).
    fake_http.route(
        "/destinations/GENAICORE",
        _dest_payload(
            {
                "URL": "https://api.ai.example.com/v2",
                "Authentication": "OAuth2ClientCredentials",
                "ProxyType": "Internet",
                "clientId": "ai-id",
                "clientSecret": "ai-secret",
                "tokenServiceURL": "https://ai-uaa.example.com",
            },
            auth_tokens=[{"error": "failed to get CSRF token", "http_header": {"key": "Authorization", "value": ""}}],
        ),
    )
    fake_http.route("//ai-uaa.example.com/oauth/token", {"access_token": "ai-bearer", "expires_in": 3600})

    resolved = d.resolve_destination("GENAICORE")
    assert resolved.url == "https://api.ai.example.com/v2"  # /v2 preserved, not doubled
    assert resolved.bearer_token == "ai-bearer"
    assert any("ai-uaa.example.com/oauth/token" in url for _, url in fake_http.requests)


def test_oauth_uses_valid_auth_token_when_present(fake_http):
    fake_http.route(
        "/destinations/GENAICORE",
        _dest_payload(
            {"URL": "https://api.ai.example.com/v2", "Authentication": "OAuth2ClientCredentials", "ProxyType": "Internet"},
            auth_tokens=[{"http_header": {"key": "Authorization", "value": "Bearer good-token"}, "expires_in": 3600}],
        ),
    )
    resolved = d.resolve_destination("GENAICORE")
    assert resolved.bearer_token == "good-token"


def test_result_is_cached_between_calls(fake_http):
    fake_http.route(
        "/destinations/S43",
        _dest_payload({"URL": "https://s4.example.com", "Authentication": "NoAuthentication", "ProxyType": "Internet"}),
    )
    d.resolve_destination("S43")
    n = len(fake_http.requests)
    d.resolve_destination("S43")
    assert len(fake_http.requests) == n  # served from cache


def test_principal_propagation_onprem_is_rejected_clearly(fake_http):
    fake_http.route(
        "/destinations/S43",
        _dest_payload(
            {"URL": "https://s4.internal", "Authentication": "PrincipalPropagation", "ProxyType": "OnPremise"}
        ),
    )
    with pytest.raises(d.DestinationError, match="PrincipalPropagation"):
        d.resolve_destination("S43")
