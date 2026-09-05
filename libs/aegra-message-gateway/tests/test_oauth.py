from datetime import UTC, datetime, timedelta

import httpx
import pytest
from aegra_message_gateway.oauth import KeycloakDeviceClient, OAuthPendingError
from aegra_message_gateway.settings import GatewaySettings


@pytest.mark.asyncio
async def test_device_flow_rewrites_browser_url(settings: GatewaySettings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/protocol/openid-connect/auth/device")
        return httpx.Response(
            200,
            json={
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "http://keycloak.test/realms/aegra/device",
                "verification_uri_complete": "http://keycloak.test/realms/aegra/device?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KeycloakDeviceClient(settings, http)
    authorization = await client.begin()

    assert authorization.verification_uri == "https://login.test/realms/aegra/device"
    assert authorization.verification_uri_complete == "https://login.test/realms/aegra/device?user_code=ABCD-EFGH"
    assert authorization.expires_at > datetime.now(UTC) + timedelta(minutes=9)
    await http.aclose()


@pytest.mark.asyncio
async def test_device_exchange_maps_pending_error(settings: GatewaySettings) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(400, json={"error": "authorization_pending"}))
    )
    client = KeycloakDeviceClient(settings, http)

    with pytest.raises(OAuthPendingError):
        await client.exchange_device_code("pending")
    await http.aclose()
