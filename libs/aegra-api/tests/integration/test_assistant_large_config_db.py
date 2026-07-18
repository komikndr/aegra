"""Regression test for assistant configs larger than PostgreSQL btree entries."""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aegra_api.core.orm import Assistant as AssistantORM
from aegra_api.settings import settings

_ASSISTANT_ID = "large-config-index-regression"


def _large_system_prompt() -> str:
    """Return deterministic text well above PostgreSQL's btree tuple limit."""
    return "\n".join(hashlib.sha256(f"prompt-{index}".encode()).hexdigest() for index in range(512))


@pytest.mark.asyncio
async def test_insert_assistant_with_large_config_succeeds() -> None:
    engine = create_async_engine(settings.db.database_url)
    try:
        try:
            async with engine.begin() as connection:
                table = await connection.scalar(text("SELECT to_regclass('public.assistant')"))
        except (OperationalError, OSError) as exc:
            pytest.skip(f"PostgreSQL test database is unavailable: {exc}")
        if table is None:
            pytest.skip("assistant table is unavailable")

        prompt = _large_system_prompt()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await session.execute(delete(AssistantORM).where(AssistantORM.assistant_id == _ASSISTANT_ID))
            await session.commit()
            session.add(
                AssistantORM(
                    assistant_id=_ASSISTANT_ID,
                    name="Large Config Regression",
                    graph_id="large-config-test-graph",
                    config={"configurable": {"system_prompt": prompt, "request_id": str(uuid4())}},
                    context={},
                    user_id="large-config-test-user",
                    metadata_dict={"test": "large-config-index"},
                    version=1,
                )
            )
            await session.commit()
            saved = await session.get(AssistantORM, _ASSISTANT_ID)
            assert saved is not None
            assert saved.config["configurable"]["system_prompt"] == prompt
            await session.execute(delete(AssistantORM).where(AssistantORM.assistant_id == _ASSISTANT_ID))
            await session.commit()
    finally:
        await engine.dispose()
