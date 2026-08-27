"""Resolve BTP destinations via the Destination service.

Both downstream systems — SAP AI Core / GenAI Hub (``GENAICORE``) and S/4HANA
(``S43``) — are reached through BTP *destinations*, not direct service
bindings. On Cloud Foundry the Destination service is bound as the
``destination-service`` instance (``VCAP_SERVICES``). We authenticate to it
with its own client credentials, then ask it to resolve a named destination
into a base URL plus ready-to-use auth headers.

Locally, set ``DESTINATION_SERVICE_KEY`` (and, for on-premise destinations,
``CONNECTIVITY_SERVICE_KEY``) to the JSON of a ``cf service-key`` output.

Tenant-specific quirks handled here (all learned from the sibling build):

* Use ``/destination-configuration/v1/destinations/{name}`` — ``/v2/`` 404s in
  this subaccount even though SAP's own Python SDK calls it.
* The Destination service's auto-fetch of ``OAuth2ClientCredentials`` tokens
  can come back empty / with a CSRF error while the destination itself is
  fine. Fall back to doing the client-credentials exchange ourselves from the
  raw ``clientId`` / ``clientSecret`` / ``tokenServiceURL`` still present in
  ``destinationConfiguration``.
* A destination ``URL`` may already carry a version segment (``.../v2``) — the
  caller is responsible for not doubling it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import httpx

from ahf_finance_agent.config import get_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
# Fallback lifetime for a resolved destination when the token response carries
# no ``expires_in`` — short enough to stay ahead of a 3600s AI Core token.
_DEFAULT_TTL_SECONDS = 600.0
# Refresh this many seconds before the known expiry.
_EXPIRY_SKEW_SECONDS = 60.0


class DestinationError(RuntimeError):
    """Raised when a BTP destination cannot be resolved or called."""


@dataclass
class ResolvedDestination:
    name: str
    url: str
    headers: dict[str, str]
    proxy: dict | None
    authentication: str
    proxy_type: str
    _expires_at: float = field(default=0.0, repr=False)

    @property
    def bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    @property
    def expired(self) -> bool:
        return self._expires_at > 0 and time.monotonic() >= self._expires_at


# --- caches -----------------------------------------------------------------
_lock = threading.Lock()
_dest_cache: dict[str, ResolvedDestination] = {}
_ds_token_cache: dict[str, tuple[str, float]] = {}  # creds-hash -> (token, exp)


def _load_service_creds(env_key: str, vcap_label: str, human_name: str) -> dict:
    raw = getattr(get_settings(), env_key.lower(), None) or os.getenv(env_key)
    if raw:
        return json.loads(raw)
    vcap = json.loads(os.getenv("VCAP_SERVICES", "{}"))
    bindings = vcap.get(vcap_label, [])
    if not bindings:
        raise DestinationError(
            f"No {human_name} binding found. Bind the '{vcap_label}' service "
            f"instance to this app on Cloud Foundry, or set {env_key} for local dev."
        )
    return bindings[0]["credentials"]


def _client_credentials_token(token_url: str, client_id: str, client_secret: str) -> tuple[str, float]:
    """Do an OAuth2 client-credentials exchange. Returns (token, expires_in)."""
    resp = httpx.post(
        f"{token_url.rstrip('/')}/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=_HTTP_TIMEOUT,
    )
    if resp.is_error:
        logger.error("Token fetch failed: url=%s status=%s body=%s", token_url, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], float(body.get("expires_in", _DEFAULT_TTL_SECONDS))


def _destination_service_token(creds: dict) -> str:
    uaa = creds.get("uaa", creds)
    cache_key = f"{uaa['url']}|{uaa['clientid']}"
    cached = _ds_token_cache.get(cache_key)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    token, expires_in = _client_credentials_token(uaa["url"], uaa["clientid"], uaa["clientsecret"])
    _ds_token_cache[cache_key] = (token, time.monotonic() + expires_in - _EXPIRY_SKEW_SECONDS)
    return token


def _connectivity_proxy() -> dict:
    creds = _load_service_creds("CONNECTIVITY_SERVICE_KEY", "connectivity", "connectivity service")
    token, _ = _client_credentials_token(
        creds["token_service_url"], creds["clientid"], creds["clientsecret"]
    )
    host, port = creds["onpremise_proxy_host"], creds["onpremise_proxy_port"]
    logger.info("Resolved Cloud Connector on-premise proxy: %s:%s", host, port)
    return {"url": f"http://{host}:{port}", "headers": {"Proxy-Authorization": f"Bearer {token}"}}


def _resolve(name: str) -> ResolvedDestination:
    creds = _load_service_creds("DESTINATION_SERVICE_KEY", "destination", "destination service")
    token = _destination_service_token(creds)
    base = creds.get("uri", creds.get("URI"))

    resp = httpx.get(
        f"{base}/destination-configuration/v1/destinations/{name}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.is_error:
        logger.error("Destination lookup failed: name=%s status=%s body=%s", name, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    data = resp.json()

    config = data["destinationConfiguration"]
    auth_type = config.get("Authentication", "NoAuthentication")
    proxy_type = config.get("ProxyType", "Internet")
    logger.info("Resolved destination %r: proxy_type=%s auth=%s url=%s", name, proxy_type, auth_type, config.get("URL"))

    proxy = None
    if proxy_type == "OnPremise":
        if auth_type == "PrincipalPropagation":
            raise DestinationError(
                f"Destination '{name}' uses PrincipalPropagation over Cloud Connector, which needs "
                "an authenticated end-user identity to forward. This agent's A2A endpoint is "
                "unauthenticated (tracked dev gap), so there is no identity to propagate."
            )
        proxy = _connectivity_proxy()

    headers = {"Accept": "application/json"}
    ttl = _DEFAULT_TTL_SECONDS

    for auth_token in data.get("authTokens", []):
        header = auth_token.get("http_header")
        if header and header.get("value") and not auth_token.get("error"):
            headers[header["key"]] = header["value"]
            if auth_token.get("expires_in"):
                ttl = float(auth_token["expires_in"])

    if "Authorization" not in headers and auth_type == "BasicAuthentication":
        raw = f"{config.get('User', '')}:{config.get('Password', '')}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"

    if "Authorization" not in headers and auth_type == "OAuth2ClientCredentials":
        client_id = config.get("clientId")
        client_secret = config.get("clientSecret")
        token_url = config.get("tokenServiceURL")
        if not (client_id and client_secret and token_url):
            raise DestinationError(
                f"Destination '{name}' is OAuth2ClientCredentials but the Destination service "
                "returned neither a usable authToken nor raw clientId/clientSecret/tokenServiceURL "
                "to fall back on."
            )
        logger.warning(
            "Destination %r: authTokens unusable (known CSRF auto-fetch bug); doing the "
            "client-credentials exchange directly.",
            name,
        )
        access_token, expires_in = _client_credentials_token(token_url, client_id, client_secret)
        headers["Authorization"] = f"Bearer {access_token}"
        ttl = expires_in

    return ResolvedDestination(
        name=name,
        url=config["URL"].rstrip("/"),
        headers=headers,
        proxy=proxy,
        authentication=auth_type,
        proxy_type=proxy_type,
        _expires_at=time.monotonic() + max(ttl - _EXPIRY_SKEW_SECONDS, 30.0),
    )


def resolve_destination(name: str, *, force_refresh: bool = False) -> ResolvedDestination:
    """Resolve a BTP destination by name. Cached until its token nears expiry."""
    with _lock:
        cached = _dest_cache.get(name)
        if cached and not cached.expired and not force_refresh:
            return cached
        resolved = _resolve(name)
        _dest_cache[name] = resolved
        return resolved


def clear_cache() -> None:
    """Drop all cached destinations / tokens (tests, or after a known rotation)."""
    with _lock:
        _dest_cache.clear()
        _ds_token_cache.clear()
