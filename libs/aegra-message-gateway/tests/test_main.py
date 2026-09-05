import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aegra_message_gateway.main import health
from fastapi import Request


@pytest.mark.asyncio
async def test_health_reports_component_readiness() -> None:
    container = SimpleNamespace(
        database=SimpleNamespace(ping=AsyncMock()),
        aegra=SimpleNamespace(health=AsyncMock()),
        chart_renderer=SimpleNamespace(health=AsyncMock()),
        telegram=SimpleNamespace(ready=True),
    )
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container))))

    response = await health(request)

    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "status": "healthy",
        "database": True,
        "aegra": True,
        "chart_renderer": True,
        "telegram": True,
    }


@pytest.mark.asyncio
async def test_health_fails_when_a_dependency_is_unavailable() -> None:
    database = SimpleNamespace(ping=AsyncMock(side_effect=RuntimeError("offline")))
    container = SimpleNamespace(
        database=database,
        aegra=SimpleNamespace(health=AsyncMock()),
        chart_renderer=SimpleNamespace(health=AsyncMock()),
        telegram=SimpleNamespace(ready=False),
    )
    request = cast(Any, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container))))

    response = await health(request)

    assert response.status_code == 503
    assert json.loads(bytes(response.body))["database"] is False
