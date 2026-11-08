"""SuperAdmin, owner-scoped virtual-ISK portfolios and bounded execution loops."""

from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.operations_access import require_operations_access
from aegra_api.core.orm import get_session
from aegra_api.core.paper_orm import PaperAccount, PaperCycle, PaperLedger
from aegra_api.models.auth import User
from aegra_api.models.paper_trading import PaperAccountCreate, PaperLoopStart, PaperQuoteInput
from aegra_api.services.paper_broker import valuation
from aegra_api.services.paper_loop import configure_loop, current_quotes, owned_account, refresh_market, store_quote
from aegra_api.services.swarm_coordination import owned_objective

router = APIRouter(prefix="/swarm/v1/paper", tags=["paper-trading"], dependencies=[Depends(require_operations_access)])


async def account_view(session: AsyncSession, account: PaperAccount) -> dict[str, Any]:
    quotes = await current_quotes(session, account)
    equity = valuation(account.cash, account.positions, quotes)
    return {
        "account_id": account.account_id,
        "simulation": True,
        "policy": account.policy,
        "cash_isk": str(account.cash),
        "positions": account.positions,
        "bid_mark_equity_isk": str(equity) if equity is not None else None,
        "net_mark_return_pct": str((equity / Decimal(account.policy["starting_isk"]) - 1) * 100)
        if equity is not None
        else None,
        "enabled": account.enabled,
        "remaining_cycles": account.remaining,
        "next_due": account.next_due,
        "quotes": quotes,
    }


@router.post("/accounts")
async def create_account(
    request: PaperAccountCreate,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    policy = request.model_dump(mode="json", exclude={"request_key"})
    if request.objective_id is not None:
        await owned_objective(session, request.objective_id, user)
    await session.execute(
        insert(PaperAccount)
        .values(
            account_id=str(uuid4()),
            user_id=user.identity,
            request_key=request.request_key,
            policy=policy,
            cash=request.starting_isk,
            positions={},
            sequence=0,
            enabled=False,
            remaining=0,
            interval_seconds=300,
            refresh_esi=False,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "request_key"])
    )
    account = await session.scalar(
        select(PaperAccount)
        .where(
            PaperAccount.user_id == user.identity,
            PaperAccount.request_key == request.request_key,
        )
        .with_for_update()
    )
    assert account is not None
    if account.policy != policy:
        raise HTTPException(409, "Request key already identifies different account settings.")
    await session.commit()
    return await account_view(session, account)


@router.get("/accounts")
async def list_accounts(
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(20, ge=1, le=100),
    objective_id: str | None = Query(None, max_length=128),
) -> list[dict[str, Any]]:
    query = select(PaperAccount).where(PaperAccount.user_id == user.identity)
    if objective_id is not None:
        await owned_objective(session, objective_id, user)
        query = query.where(PaperAccount.policy["objective_id"].as_string() == objective_id)
    rows = (
        await session.scalars(
            query
            .order_by(PaperAccount.account_id)
            .limit(limit)
        )
    ).all()
    return [await account_view(session, row) for row in rows]


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: str,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await account_view(session, await owned_account(session, account_id, user))


@router.post("/accounts/{account_id}/quotes")
async def add_fixture_quote(
    account_id: str,
    request: PaperQuoteInput,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    account = await owned_account(session, account_id, user, lock=True)
    quote = await store_quote(session, account, request, source="manual_fixture")
    await session.commit()
    return {"quote_id": quote.quote_id, "simulation": True, "expires_at": quote.expires_at}


@router.post("/accounts/{account_id}/quotes/refresh")
async def refresh_quotes(account_id: str, user: User = Depends(require_operations_access)) -> dict[str, Any]:
    try:
        await refresh_market(account_id, user)
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(502, "Public ESI market collection failed; no fabricated quotes were created.") from error
    return {"refreshed": True, "simulation": True}


@router.post("/accounts/{account_id}/start")
async def start_loop(
    account_id: str,
    request: PaperLoopStart,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    account = await owned_account(session, account_id, user, lock=True)
    await configure_loop(session, account, request)
    await session.commit()
    return await account_view(session, account)


@router.post("/accounts/{account_id}/stop")
async def stop_loop(
    account_id: str,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    account = await owned_account(session, account_id, user, lock=True)
    account.enabled, account.remaining = False, 0
    await session.commit()
    return await account_view(session, account)


@router.get("/accounts/{account_id}/ledger")
async def get_ledger(
    account_id: str,
    *,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_account(session, account_id, user)
    rows = (
        await session.scalars(
            select(PaperLedger)
            .where(PaperLedger.account_id == account_id)
            .order_by(PaperLedger.created_at, PaperLedger.cycle_id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [{"cycle_id": row.cycle_id, "created_at": row.created_at, **row.entry} for row in rows]


@router.get("/accounts/{account_id}/cycles")
async def get_cycles(
    account_id: str,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_account(session, account_id, user)
    rows = (
        await session.scalars(
            select(PaperCycle).where(PaperCycle.account_id == account_id).order_by(PaperCycle.number.desc()).limit(100)
        )
    ).all()
    return [
        {
            "cycle_id": row.cycle_id,
            "number": row.number,
            "status": row.status,
            "run_id": row.run_id,
            "result": row.result,
            "deadline": row.deadline,
        }
        for row in rows
    ]


@router.get("/accounts/{account_id}/cycles/{cycle_id}")
async def get_cycle(
    account_id: str, cycle_id: str,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await owned_account(session, account_id, user)
    cycle = await session.scalar(select(PaperCycle).where(PaperCycle.account_id == account_id, PaperCycle.cycle_id == cycle_id))
    if cycle is None:
        raise HTTPException(404, "Paper cycle not found.")
    return {"cycle_id": cycle.cycle_id, "number": cycle.number, "status": cycle.status,
            "run_id": cycle.run_id, "input_snapshot": cycle.snapshot, "result": cycle.result}
