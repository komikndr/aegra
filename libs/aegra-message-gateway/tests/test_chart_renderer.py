import json

import httpx
import pytest
from aegra_message_gateway.chart_renderer import ChartRendererClient, ChartRenderError
from aegra_message_gateway.settings import GatewaySettings


@pytest.mark.asyncio
async def test_render_returns_valid_png_response(settings: GatewaySettings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/render"
        assert json.loads(request.content) == {"option": {"series": []}}
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nimage", headers={"content-type": "image/png"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ChartRendererClient(settings, http)

    assert await client.render({"series": []}) == b"\x89PNG\r\n\x1a\nimage"
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (422, "application/json", b'{"error":"invalid"}'),
        (200, "text/plain", b"not a png"),
        (200, "image/png", b""),
    ],
)
async def test_render_rejects_invalid_renderer_responses(
    settings: GatewaySettings,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ChartRendererClient(settings, http)

    with pytest.raises(ChartRenderError):
        await client.render({"series": []})
    await http.aclose()


@pytest.mark.asyncio
async def test_renderer_network_failures_are_normalized(settings: GatewaySettings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ChartRendererClient(settings, http)

    with pytest.raises(ChartRenderError):
        await client.render({"series": []})
    with pytest.raises(ChartRenderError):
        await client.health()
    await http.aclose()
