"""Authorization-aware service base."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.auth_handlers import build_auth_context, handle_event
from aegra_api.models.auth import User


class Authenticated:
    """Carry the request identity and dispatch authorization at the service boundary."""

    resource: str

    def __init__(self, session: AsyncSession, user: User) -> None:
        self.session = session
        self.user = user

    async def _dispatch(self, action: str, value: dict[str, Any]) -> dict[str, Any] | None:
        return await handle_event(build_auth_context(self.user, self.resource, action), value)
