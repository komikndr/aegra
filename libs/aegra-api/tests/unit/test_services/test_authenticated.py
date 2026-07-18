"""Tests for service-layer authorization dispatch."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from aegra_api.models.auth import User
from aegra_api.services.authenticated import Authenticated


class _Service(Authenticated):
    resource = "assistants"


@pytest.fixture
def service() -> _Service:
    return _Service(AsyncMock(), User(identity="user-123"))


@pytest.mark.asyncio
async def test_dispatch_builds_context_and_returns_filters(service: _Service) -> None:
    filters = {"metadata": {"team": "onyx"}}
    with patch("aegra_api.services.authenticated.handle_event", new=AsyncMock(return_value=filters)) as handler:
        result = await service._dispatch("search", {"name": "agent"})

    context = handler.await_args.args[0]
    assert context.resource == "assistants"
    assert context.action == "search"
    assert context.user.identity == "user-123"
    assert result == filters


@pytest.mark.asyncio
async def test_dispatch_propagates_denial(service: _Service) -> None:
    denial = HTTPException(status_code=403, detail="Forbidden")
    with (
        patch("aegra_api.services.authenticated.handle_event", new=AsyncMock(side_effect=denial)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await service._dispatch("delete", {"assistant_id": "a-1"})
    assert exc_info.value.status_code == 403
