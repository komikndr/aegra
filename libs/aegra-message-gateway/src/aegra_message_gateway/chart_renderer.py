"""Internal ECharts PNG renderer client."""

from typing import Any

import httpx

from aegra_message_gateway.settings import GatewaySettings


class ChartRenderError(Exception):
    """The chart option could not be rendered safely."""


class ChartRendererClient:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.chart_renderer_timeout_seconds)
        self._owns_client = client is None

    async def render(self, option: dict[str, Any]) -> bytes:
        try:
            response = await self._client.post(f"{self._settings.chart_renderer_url}/render", json={"option": option})
        except httpx.HTTPError as exc:
            raise ChartRenderError("chart renderer is unavailable") from exc
        if response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0] != "image/png":
            raise ChartRenderError("chart renderer rejected the option")
        image = response.content
        if not image or len(image) > self._settings.chart_renderer_max_image_bytes:
            raise ChartRenderError("chart renderer returned an invalid image")
        return image

    async def health(self) -> None:
        try:
            response = await self._client.get(f"{self._settings.chart_renderer_url}/health")
        except httpx.HTTPError as exc:
            raise ChartRenderError("chart renderer is unavailable") from exc
        if response.status_code != 200:
            raise ChartRenderError("chart renderer is unavailable")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
