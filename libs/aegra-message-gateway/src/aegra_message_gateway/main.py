"""FastAPI application for the direct Telegram message gateway."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aegra_message_gateway.aegra_client import AegraClient
from aegra_message_gateway.chart_renderer import ChartRendererClient
from aegra_message_gateway.db import Database
from aegra_message_gateway.oauth import KeycloakDeviceClient
from aegra_message_gateway.repository import GatewayRepository
from aegra_message_gateway.service import GatewayService
from aegra_message_gateway.settings import GatewaySettings, get_settings
from aegra_message_gateway.telegram import TelegramGateway

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: GatewaySettings
    database: Database
    repository: GatewayRepository
    oauth: KeycloakDeviceClient
    aegra: AegraClient
    chart_renderer: ChartRendererClient
    service: GatewayService
    telegram: TelegramGateway


def create_container(settings: GatewaySettings) -> Container:
    database = Database(settings)
    oauth = KeycloakDeviceClient(settings)
    aegra = AegraClient(settings)
    chart_renderer = ChartRendererClient(settings)
    repository = GatewayRepository(database, processing_lease_seconds=settings.inbound_processing_lease_seconds)
    service = GatewayService(
        settings=settings,
        repository=repository,
        oauth=oauth,
        aegra=aegra,
        chart_renderer=chart_renderer,
    )
    telegram = TelegramGateway(settings=settings, service=service)
    return Container(
        settings=settings,
        database=database,
        repository=repository,
        oauth=oauth,
        aegra=aegra,
        chart_renderer=chart_renderer,
        service=service,
        telegram=telegram,
    )


async def _maintenance(container: Container) -> None:
    while True:
        try:
            await container.repository.cleanup_expired(inbound_retention_days=container.settings.inbound_retention_days)
        except Exception:
            logger.exception("Message gateway maintenance failed")
        await asyncio.sleep(container.settings.maintenance_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = create_container(get_settings())
    app.state.container = container
    maintenance_task: asyncio.Task[None] | None = None
    try:
        await container.database.ping()
        await container.telegram.start()
        maintenance_task = asyncio.create_task(_maintenance(container), name="message-gateway-maintenance")
        yield
    finally:
        if maintenance_task is not None:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
        try:
            await container.telegram.stop()
        except Exception:
            logger.exception("Failed to stop Telegram transport cleanly")
        await asyncio.gather(
            container.oauth.close(),
            container.aegra.close(),
            container.chart_renderer.close(),
            container.database.close(),
            return_exceptions=True,
        )


app = FastAPI(title="Aegra Message Gateway", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    container: Container = request.app.state.container
    database_ready = True
    aegra_ready = True
    chart_renderer_ready = True
    try:
        await container.database.ping()
    except Exception:
        database_ready = False
    try:
        await container.aegra.health()
    except Exception:
        aegra_ready = False
    try:
        await container.chart_renderer.health()
    except Exception:
        chart_renderer_ready = False
    telegram_ready = container.telegram.ready
    healthy = database_ready and aegra_ready and chart_renderer_ready and telegram_ready
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "unavailable",
            "database": database_ready,
            "aegra": aegra_ready,
            "chart_renderer": chart_renderer_ready,
            "telegram": telegram_ready,
        },
    )


def run() -> None:
    uvicorn.run("aegra_message_gateway.main:app", host="0.0.0.0", port=8642)
