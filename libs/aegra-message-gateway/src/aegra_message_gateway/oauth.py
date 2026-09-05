"""Keycloak OAuth 2 device authorization and token refresh client."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from aegra_message_gateway.settings import GatewaySettings


class OAuthPendingError(Exception):
    pass


class OAuthSlowDownError(Exception):
    pass


class OAuthExpiredError(Exception):
    pass


class OAuthDeniedError(Exception):
    pass


class OAuthTokenError(Exception):
    pass


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: datetime
    interval_seconds: int


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


class KeycloakDeviceClient:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._owns_client = client is None

    @property
    def _device_endpoint(self) -> str:
        return f"{self._settings.keycloak_internal_issuer}/protocol/openid-connect/auth/device"

    @property
    def _token_endpoint(self) -> str:
        return f"{self._settings.keycloak_internal_issuer}/protocol/openid-connect/token"

    @property
    def _userinfo_endpoint(self) -> str:
        return f"{self._settings.keycloak_internal_issuer}/protocol/openid-connect/userinfo"

    @property
    def _revoke_endpoint(self) -> str:
        return f"{self._settings.keycloak_internal_issuer}/protocol/openid-connect/revoke"

    async def begin(self) -> DeviceAuthorization:
        response = await self._client.post(
            self._device_endpoint,
            data={"client_id": self._settings.keycloak_client_id, "scope": self._settings.keycloak_scopes},
        )
        response.raise_for_status()
        payload = response.json()
        expires_in = max(60, int(payload["expires_in"]))
        interval = max(1, int(payload.get("interval", 5)))
        verification_uri = self._browser_url(str(payload["verification_uri"]))
        verification_complete = payload.get("verification_uri_complete")
        return DeviceAuthorization(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=verification_uri,
            verification_uri_complete=self._browser_url(str(verification_complete)) if verification_complete else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            interval_seconds=interval,
        )

    async def exchange_device_code(self, device_code: str) -> OAuthTokens:
        return await self._token_request(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self._settings.keycloak_client_id,
                "device_code": device_code,
            }
        )

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._settings.keycloak_client_id,
                "refresh_token": refresh_token,
            }
        )

    async def userinfo(self, access_token: str) -> dict[str, object]:
        response = await self._client.get(
            self._userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthTokenError("userinfo response was invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
            raise OAuthTokenError("userinfo response did not contain a subject")
        return payload

    async def revoke(self, refresh_token: str) -> None:
        response = await self._client.post(
            self._revoke_endpoint,
            data={
                "client_id": self._settings.keycloak_client_id,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _token_request(self, data: dict[str, str]) -> OAuthTokens:
        response = await self._client.post(self._token_endpoint, data=data)
        if response.status_code >= 400:
            self._raise_token_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthTokenError("token response was invalid") from exc
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise OAuthTokenError("token response was incomplete")
        expires_in = max(30, int(payload.get("expires_in", 60)))
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )

    @staticmethod
    def _raise_token_error(response: httpx.Response) -> None:
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if error == "authorization_pending":
            raise OAuthPendingError
        if error == "slow_down":
            raise OAuthSlowDownError
        if error == "expired_token":
            raise OAuthExpiredError
        if error == "access_denied":
            raise OAuthDeniedError
        raise OAuthTokenError("OAuth token request failed")

    def _browser_url(self, url: str) -> str:
        internal = self._settings.keycloak_internal_issuer
        if url.startswith(internal):
            return self._settings.keycloak_browser_issuer + url[len(internal) :]
        return url


class AccessTokenCache:
    """Small process-local token cache with per-link refresh serialization."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, link_id: str) -> str | None:
        cached = self._tokens.get(link_id)
        if cached is None:
            return None
        token, expires_at = cached
        if expires_at <= datetime.now(UTC) + timedelta(seconds=30):
            self._tokens.pop(link_id, None)
            return None
        return token

    def put(self, link_id: str, token: str, expires_at: datetime) -> None:
        self._tokens[link_id] = (token, expires_at)

    def remove(self, link_id: str) -> None:
        self._tokens.pop(link_id, None)

    def lock(self, link_id: str) -> asyncio.Lock:
        return self._locks.setdefault(link_id, asyncio.Lock())
