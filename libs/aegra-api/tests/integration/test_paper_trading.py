"""Isolated PostgreSQL paper-ledger and dispatch recovery checks."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aegra_api.api.paper_trading import router
from aegra_api.core.auth_deps import require_auth
from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.orm import Assistant, Base, Run, Thread, get_session
from aegra_api.core.paper_orm import PaperAccount, PaperCommand, PaperCycle, PaperLedger, PaperQuote
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmStrategyVersion, SwarmTask
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor, SwarmSupervisorCommand
from aegra_api.models.auth import User
from aegra_api.models.swarm_coordination import ObjectiveCreate
from aegra_api.services import paper_loop, swarm_dispatch
from aegra_api.services.swarm_coordination import create_objective

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def setup(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession], FastAPI]]:
    url = os.environ.get("SWARM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SWARM_TEST_DATABASE_URL for disposable-schema paper tests.")
    schema = "paper_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, execution_options={"schema_translate_map": {None: schema}})
    tables = [
        Assistant.__table__,
        Thread.__table__,
        Run.__table__,
        PaperAccount.__table__,
        PaperQuote.__table__,
        PaperCycle.__table__,
        PaperLedger.__table__,
        PaperCommand.__table__,
        SwarmObjective.__table__,
        SwarmReview.__table__,
        SwarmEntry.__table__,
        SwarmStrategyVersion.__table__,
        SwarmTask.__table__,
        CoordinatorTurn.__table__,
        SwarmSupervisor.__table__,
        SwarmSupervisorCommand.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(paper_loop, "_get_session_maker", lambda: maker)
    monkeypatch.setattr(paper_loop.executor, "submit", AsyncMock())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["SuperAdmin"])

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, maker, app
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def account(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/swarm/v1/paper/accounts",
        json={
            "request_key": "account",
            "name": "Synthetic paper test",
            "starting_isk": "10000",
            "type_ids": [34],
            "slippage_bps": 0,
            "max_position_bps": 10000,
        },
    )
    assert response.status_code == 200, response.text
    account_id = response.json()["account_id"]
    response = await client.post(
        f"/swarm/v1/paper/accounts/{account_id}/quotes",
        json={
            "type_id": 34,
            "bid": "10",
            "ask": "11",
            "bid_quantity": 1000,
            "ask_quantity": 1000,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return account_id


async def begin_cycle(client: httpx.AsyncClient, account_id: str, key: str) -> None:
    response = await client.post(f"/swarm/v1/paper/accounts/{account_id}/start", json={"request_key": key})
    assert response.status_code == 200, response.text
    await paper_loop.tick_account(account_id)


async def finish(maker: async_sessionmaker[AsyncSession], account_id: str, action: str) -> str:
    async with maker() as session, session.begin():
        cycle = await session.scalar(
            select(PaperCycle).where(PaperCycle.account_id == account_id, PaperCycle.status == "pending")
        )
        assert cycle is not None
        run = await session.get(Run, cycle.run_id)
        assert run is not None
        run.status = "success"
        run.output = {
            "decision": {
                "action": action,
                "type_id": 34,
                "quantity": 100,
                "limit_price": "11" if action == "buy" else "10",
                "reason": "Injected test decision, not live inference",
            }
        }
        return cycle.cycle_id


async def test_duplicate_delivery_cannot_double_fill_and_sell_pnl_is_deterministic(setup: Any) -> None:
    client, maker, _ = setup
    account_id = await account(client)
    await begin_cycle(client, account_id, "buy")
    await finish(maker, account_id, "buy")
    await asyncio.gather(
        paper_loop.tick_account(account_id), paper_loop.tick_account(account_id), paper_loop.tick_account(account_id)
    )
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(PaperLedger)) == 1
        saved = await session.get(PaperAccount, account_id)
        assert saved.cash == Decimal("8889")
    # Retrying the same start key after completion never restarts the loop.
    await client.post(f"/swarm/v1/paper/accounts/{account_id}/start", json={"request_key": "buy"})
    await paper_loop.tick_account(account_id)
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(PaperCycle)) == 1
    await begin_cycle(client, account_id, "sell")
    await finish(maker, account_id, "sell")
    await paper_loop.tick_account(account_id)
    response = await client.get(f"/swarm/v1/paper/accounts/{account_id}")
    assert Decimal(response.json()["cash_isk"]) == Decimal(9879)
    response = await client.get(f"/swarm/v1/paper/accounts/{account_id}/ledger")
    assert response.json()[1]["realized_pnl"] == "-121.0000"


@pytest.mark.parametrize("stop", [True, False])
async def test_stop_or_expired_quote_blocks_late_success(setup: Any, stop: bool) -> None:
    client, maker, _ = setup
    account_id = await account(client)
    await begin_cycle(client, account_id, "late")
    await finish(maker, account_id, "buy")
    if stop:
        await client.post(f"/swarm/v1/paper/accounts/{account_id}/stop")
    else:
        async with maker() as session, session.begin():
            cycle = await session.scalar(select(PaperCycle).where(PaperCycle.account_id == account_id))
            snapshot = {
                **cycle.snapshot,
                "quotes": {
                    key: {**value, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
                    for key, value in cycle.snapshot["quotes"].items()
                },
            }
            cycle.snapshot = snapshot
    await paper_loop.tick_account(account_id)
    async with maker() as session:
        saved = await session.get(PaperAccount, account_id)
        assert saved.cash == 10000 and saved.positions == {}
        assert (await session.scalar(select(PaperLedger))).entry["status"] == ("cancelled" if stop else "rejected")


async def test_committed_dispatch_intent_retries_same_run(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, maker, _ = setup
    account_id = await account(client)
    await begin_cycle(client, account_id, "recovery")
    submit = AsyncMock(side_effect=OSError("queue unavailable"))
    monkeypatch.setattr(paper_loop.executor, "submit", submit)
    with pytest.raises(OSError):
        await paper_loop.tick_account(account_id)
    async with maker() as session, session.begin():
        cycle = await session.scalar(select(PaperCycle))
        original = cycle.run_id
        cycle.submitted_at = datetime.now(UTC) - timedelta(seconds=31)
    submit.side_effect = None
    await paper_loop.tick_account(account_id)
    assert submit.await_count == 2 and submit.await_args.args[0].identity.run_id == original


async def test_role_and_owner_boundaries(setup: Any) -> None:
    client, _, app = setup
    account_id = await account(client)
    app.dependency_overrides[require_auth] = lambda: User(identity="bob", permissions=["SuperAdmin"])
    assert (await client.get(f"/swarm/v1/paper/accounts/{account_id}")).status_code == 404
    app.dependency_overrides[require_auth] = lambda: User(identity="alice", permissions=["Executive"])
    assert (
        await client.post(f"/swarm/v1/paper/accounts/{account_id}/start", json={"request_key": "forbidden"})
    ).status_code == 403


async def test_paper_local_restart_reuses_run_and_durable_intent(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, maker, _ = setup
    account_id = await account(client)
    await begin_cycle(client, account_id, "restart")
    async with maker() as session, session.begin():
        cycle = await session.scalar(select(PaperCycle))
        run_id = cycle.run_id
        run = await session.get(Run, run_id)
        run.status = "running"
        cycle.submitted_at = datetime.now(UTC)
    monkeypatch.setattr(swarm_dispatch, "_get_session_maker", lambda: maker)
    monkeypatch.setattr(swarm_dispatch.settings.redis, "REDIS_BROKER_ENABLED", False)
    await swarm_dispatch.recover_local_runs()
    async with maker() as session:
        cycle = await session.scalar(select(PaperCycle))
        assert cycle.run_id == run_id and cycle.submitted_at is None
        assert (await session.get(Run, run_id)).status == "pending"
        assert await session.scalar(select(func.count()).select_from(PaperCycle)) == 1


async def test_linked_objective_board_receives_execution_and_pause_prevents_fill(setup: Any) -> None:
    client, maker, _ = setup
    user = User(identity="alice", permissions=["SuperAdmin"])
    async with maker() as session, session.begin():
        objective = await create_objective(
            session,
            user,
            ObjectiveCreate(
                request_key="mission",
                title="Paper mission",
                goal="Assess simulated opportunities",
                success_criteria="Do not guarantee returns",
                evidence=(),
            ),
        )
        objective_id = objective.objective_id
    response = await client.post(
        "/swarm/v1/paper/accounts",
        json={
            "request_key": "linked",
            "name": "Linked paper account",
            "starting_isk": "10000",
            "type_ids": [34],
            "objective_id": objective_id,
        },
    )
    account_id = response.json()["account_id"]
    await begin_cycle(client, account_id, "pause-race")
    await finish(maker, account_id, "buy")
    async with maker() as session, session.begin():
        objective = await session.get(SwarmObjective, objective_id)
        objective.status = "paused"
    await paper_loop.tick_account(account_id)
    async with maker() as session:
        entry = await session.scalar(select(SwarmEntry).where(SwarmEntry.kind == "paper_execution"))
        assert entry.content["status"] == "cancelled"
        objective = await session.get(SwarmObjective, objective_id)
        assert objective.current_evidence[-1]["simulation"] is True
        assert (await session.get(PaperAccount, account_id)).cash == 10000
