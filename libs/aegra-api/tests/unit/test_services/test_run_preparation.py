"""Focused tests for run preparation race handling."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from aegra_api.services.run_preparation import _validate_resume_command


class _Session:
    def __init__(self, thread: Any) -> None:
        self.thread = thread

    async def scalar(self, _statement: Any) -> Any:
        return self.thread

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_resume_none_is_still_validated() -> None:
    idle = SimpleNamespace(status="idle")

    def maker() -> _Session:
        return _Session(idle)

    with (
        patch("aegra_api.services.run_preparation._get_session_maker", return_value=maker),
        patch("aegra_api.services.run_preparation._RESUME_SETTLE_ATTEMPTS", 1),
        patch("aegra_api.services.run_preparation.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(HTTPException, match="not in interrupted state"),
    ):
        await _validate_resume_command(_Session(idle), "thread-1", "user-1", {"resume": None})


@pytest.mark.asyncio
async def test_resume_waits_for_interrupted_status_on_fresh_session() -> None:
    idle = SimpleNamespace(status="idle")
    interrupted = SimpleNamespace(status="interrupted")

    def maker() -> _Session:
        return _Session(interrupted)

    with (
        patch("aegra_api.services.run_preparation._get_session_maker", return_value=maker),
        patch("aegra_api.services.run_preparation.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        await _validate_resume_command(_Session(idle), "thread-1", "user-1", {"resume": "approved"})

    sleep.assert_awaited_once()
