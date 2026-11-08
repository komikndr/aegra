"""Durable bounded paper cycles through Aegra's existing executor."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import structlog
from fastapi import HTTPException
from redis import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.active_runs import active_runs
from aegra_api.core.orm import Run, Thread, _get_session_maker
from aegra_api.core.paper_orm import PaperAccount, PaperCommand, PaperCycle, PaperLedger, PaperQuote
from aegra_api.core.swarm_orm import SwarmObjective
from aegra_api.models.auth import User
from aegra_api.models.paper_trading import PaperDecision, PaperLoopStart, PaperQuoteInput
from aegra_api.models.run_job import RunExecution, RunIdentity, RunJob
from aegra_api.services.broker import broker_manager
from aegra_api.services.eve_market import fetch_eve_history, fetch_eve_quote
from aegra_api.services.executor import executor
from aegra_api.services.paper_broker import execute_paper_decision, valuation
from aegra_api.services.swarm_coordination import append_entry
from aegra_api.settings import settings

logger = structlog.get_logger(__name__)
TERMINAL = {"success", "error", "timeout", "interrupted", "cancelled"}


async def owned_account(session: AsyncSession, account_id: str, user: User, *, lock: bool = False) -> PaperAccount:
    query = select(PaperAccount).where(PaperAccount.account_id == account_id, PaperAccount.user_id == user.identity)
    account = await session.scalar(query.with_for_update() if lock else query)
    if account is None:
        raise HTTPException(404, "Paper account not found.")
    return account


async def configure_loop(session: AsyncSession, account: PaperAccount, request: PaperLoopStart) -> None:
    """Caller holds the account lock and commits this command with its receipt."""
    prior = await session.get(PaperCommand, (account.account_id, request.request_key))
    payload = request.model_dump(mode="json")
    if prior is not None:
        if prior.request != payload:
            raise HTTPException(409, "Request key already identifies a different loop command.")
        return
    pending = await session.scalar(select(PaperCycle.cycle_id).where(
        PaperCycle.account_id == account.account_id, PaperCycle.status == "pending",
    ))
    if account.enabled or pending is not None:
        raise HTTPException(409, "A loop is active or its last cycle is still being reconciled.")
    session.add(PaperCommand(account_id=account.account_id, request_key=request.request_key, request=payload))
    account.enabled, account.remaining = True, request.cycles
    account.interval_seconds, account.refresh_esi = request.interval_seconds, request.refresh_esi
    account.next_due = datetime.now(UTC)


async def store_quote(
    session: AsyncSession,
    account: PaperAccount,
    quote: PaperQuoteInput,
    *,
    source: str,
    version: str | None = None,
    expires_at: datetime | None = None,
    history: list[dict[str, Any]] | None = None,
) -> PaperQuote:
    if quote.type_id not in account.policy["type_ids"]:
        raise HTTPException(422, "Quote type is outside the watchlist.")
    now = datetime.now(UTC)
    if quote.observed_at > now or quote.observed_at < now - timedelta(minutes=5):
        raise HTTPException(422, "Quote observation must be within the last five minutes.")
    data = quote.model_dump(mode="json")
    version = version or hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    quote_id = hashlib.sha256(f"{account.account_id}:{quote.type_id}:{version}".encode()).hexdigest()
    existing = await session.get(PaperQuote, quote_id)
    if existing is not None:
        return existing  # Re-fetching cached data cannot replenish consumed paper liquidity.
    row = PaperQuote(
        quote_id=quote_id,
        account_id=account.account_id,
        data={
            **data,
            "source": source,
            "input_simulation": source == "manual_fixture",
            "regional_daily_history": history or [],
        },
        expires_at=expires_at or quote.observed_at + timedelta(minutes=5),
        used_bid=0,
        used_ask=0,
    )
    session.add(row)
    return row


async def current_quotes(session: AsyncSession, account: PaperAccount) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(PaperQuote)
            .where(
                PaperQuote.account_id == account.account_id,
                PaperQuote.expires_at > datetime.now(UTC),
            )
            .order_by(PaperQuote.expires_at.desc(), PaperQuote.quote_id)
            .limit(300)
        )
    ).all()
    quotes: dict[str, Any] = {}
    for row in rows:
        quotes.setdefault(
            str(row.data["type_id"]),
            {
                **row.data,
                "quote_id": row.quote_id,
                "expires_at": row.expires_at.isoformat(),
                "used_bid": row.used_bid,
                "used_ask": row.used_ask,
            },
        )
    return quotes


async def refresh_market(account_id: str, user: User) -> None:
    maker = _get_session_maker()
    async with maker() as session:
        account = await owned_account(session, account_id, user)
        policy = account.policy
    fetched = []
    async with asyncio.timeout(35):
        for type_id in policy["type_ids"]:
            quote, version, expiry = await fetch_eve_quote(
                region_id=policy["region_id"], location_id=policy["location_id"], type_id=type_id
            )
            history = await fetch_eve_history(region_id=policy["region_id"], type_id=type_id)
            fetched.append((quote, version, expiry, history))
    async with maker() as session, session.begin():
        account = await owned_account(session, account_id, user, lock=True)
        for quote, version, expiry, history in fetched:
            await store_quote(
                session, account, quote, source="public_esi", version=version, expires_at=expiry, history=history
            )


async def create_cycle(session: AsyncSession, account: PaperAccount) -> PaperCycle:
    account.sequence += 1
    cycle_id, run_id, thread_id = str(uuid4()), str(uuid4()), str(uuid4())
    quotes = await current_quotes(session, account)
    recent = (
        await session.scalars(
            select(PaperLedger)
            .where(PaperLedger.account_id == account.account_id)
            .order_by(PaperLedger.created_at.desc())
            .limit(5)
        )
    ).all()
    snapshot = {
        "policy": account.policy,
        "cash": str(account.cash),
        "positions": account.positions,
        "quotes": quotes,
        "equity": str(valuation(account.cash, account.positions, quotes)),
        "recent_outcomes": [row.entry for row in recent],
        "simulation": True,
    }
    fee = Decimal(account.policy["fee_bps"]) / 10000
    slippage = Decimal(account.policy["slippage_bps"]) / 10000
    snapshot["calculated_costs"] = {
        "fee_pct_each_side": str(fee * 100), "adverse_slippage_pct_each_side": str(slippage * 100),
        "same_price_round_trip_loss_pct_before_rounding": str(100 * (1 - (1 - fee) * (1 - slippage) / ((1 + fee) * (1 + slippage)))),
    }
    if account.policy.get("objective_id"):
        objective = await session.get(SwarmObjective, account.policy["objective_id"])
        assert objective is not None
        snapshot["objective"] = {
            "goal": objective.definition["goal"],
            "success_criteria": objective.definition["success_criteria"],
            "evidence": objective.current_evidence[-8:],
            "total_evidence_count": len(objective.current_evidence),
        }
    job = RunJob(
        identity=RunIdentity(run_id=run_id, thread_id=thread_id, graph_id="paper_trader"),
        user=User(identity=account.user_id, permissions=["SuperAdmin"]),
        execution=RunExecution(input_data={"snapshot": snapshot}, config={"recursion_limit": 8}, stream_mode="values"),
        run_metadata={"paper_account_id": account.account_id, "paper_cycle_id": cycle_id},
    )
    session.add(
        Thread(
            thread_id=thread_id,
            user_id=account.user_id,
            status="idle",
            metadata_json={"paper_account_id": account.account_id},
        )
    )
    await session.flush()
    session.add(
        Run(
            run_id=run_id,
            thread_id=thread_id,
            user_id=account.user_id,
            status="pending",
            input=job.execution.input_data,
            config=job.execution.config,
            context={},
            execution_params=job.to_execution_params(),
        )
    )
    await session.flush()
    cycle = PaperCycle(
        cycle_id=cycle_id,
        account_id=account.account_id,
        number=account.sequence,
        run_id=run_id,
        status="pending",
        snapshot=snapshot,
        deadline=datetime.now(UTC) + timedelta(minutes=4),
    )
    session.add(cycle)
    account.remaining -= 1
    account.next_due = datetime.now(UTC) + timedelta(seconds=account.interval_seconds)
    return cycle


async def reconcile_cycle(session: AsyncSession, account: PaperAccount, cycle: PaperCycle, run: Run) -> None:
    if cycle.status != "pending":
        return
    result: dict[str, Any]
    if not account.enabled or cycle.deadline <= datetime.now(UTC):
        result = {"simulation": True, "status": "cancelled" if not account.enabled else "expired"}
    elif run.status not in TERMINAL:
        return
    elif run.status != "success":
        result = {"simulation": True, "status": "failed", "reason": "Model execution did not succeed."}
    else:
        try:
            decision = PaperDecision.model_validate((run.output or {}).get("decision"))
        except ValueError:
            result = {"simulation": True, "status": "failed", "reason": "Invalid model action contract."}
        else:
            # Execute against exactly the observed quote versions, but recheck
            # expiry and consumed liquidity at commit time, never trust the model.
            quotes = {}
            for type_id, original in cycle.snapshot["quotes"].items():
                quote = await session.get(PaperQuote, original["quote_id"])
                if quote is not None:
                    quotes[type_id] = {**original, "used_bid": quote.used_bid, "used_ask": quote.used_ask}
            cash, positions, result = execute_paper_decision(
                cash=account.cash, positions=account.positions, policy=account.policy, quotes=quotes, decision=decision
            )
            if result["status"] == "filled":
                quote = await session.get(PaperQuote, result["quote_id"])
                assert quote is not None
                if result["side"] == "ask":
                    quote.used_ask += result["quantity"]
                else:
                    quote.used_bid += result["quantity"]
                account.cash, account.positions = cash, positions
    marks = await current_quotes(session, account)
    marked_equity = valuation(account.cash, account.positions, marks)
    result.update({
        "cash_after": str(account.cash), "positions_after": account.positions,
        "bid_mark_equity_isk": str(marked_equity) if marked_equity is not None else None,
        "producer_model": (run.output or {}).get("model"),
    })
    cycle.status = str(result["status"])
    cycle.result = result
    session.add(PaperLedger(cycle_id=cycle.cycle_id, account_id=account.account_id, entry=result))
    if account.policy.get("objective_id"):
        objective = await session.get(SwarmObjective, account.policy["objective_id"])
        assert objective is not None  # tick_account holds the objective row lock.
        append_entry(
            session,
            objective,
            kind="paper_execution",
            author="paper_broker",
            content={"account_id": account.account_id, "cycle_id": cycle.cycle_id, **result},
        )
        evidence_id = f"paper:{account.account_id}:latest"
        others = [row for row in objective.current_evidence if row["evidence_id"] != evidence_id]
        if len(others) < 40:
            now = datetime.now(UTC).isoformat()
            objective.current_evidence = [
                *others,
                {
                    "evidence_id": evidence_id,
                    "source": "paper_broker",
                    "observed_at": now,
                    "available_at": now,
                    "content": json.dumps({"cycle_id": cycle.cycle_id, **result}),
                    "simulation": True,
                },
            ]
    if account.remaining == 0:
        account.enabled = False


async def tick_account(account_id: str) -> None:
    maker = _get_session_maker()
    job: RunJob | None = None
    cancel_run: str | None = None
    async with maker() as session, session.begin():
        account = await session.scalar(
            select(PaperAccount).where(PaperAccount.account_id == account_id).with_for_update(skip_locked=True)
        )
        if account is None:
            return
        if account.policy.get("objective_id"):
            objective = await session.scalar(
                select(SwarmObjective)
                .where(
                    SwarmObjective.objective_id == account.policy["objective_id"],
                )
                .with_for_update()
            )
            if objective is None or objective.status != "active":
                account.enabled, account.remaining = False, 0
        cycle = await session.scalar(
            select(PaperCycle).where(PaperCycle.account_id == account_id, PaperCycle.status == "pending")
        )
        if cycle is not None:
            run = await session.get(Run, cycle.run_id)
            assert run is not None
            await reconcile_cycle(session, account, cycle, run)
            if cycle.status != "pending" and run.status not in TERMINAL:
                if run.status == "pending":
                    run.status = "interrupted"
                else:
                    cancel_run = run.run_id
            elif (
                cycle.status == "pending"
                and run.status == "pending"
                and (cycle.submitted_at is None or cycle.submitted_at < datetime.now(UTC) - timedelta(seconds=30))
                and (settings.redis.REDIS_BROKER_ENABLED or run.run_id not in active_runs)
            ):
                cycle.submitted_at = datetime.now(UTC)
                job = RunJob.from_run_orm(run)
        elif (
            account.enabled
            and account.remaining > 0
            and (account.next_due is None or account.next_due <= datetime.now(UTC))
        ):
            await create_cycle(session, account)
    if cancel_run:
        await broker_manager.request_cancel(cancel_run, "cancel")
    if job is not None:
        await executor.submit(job)


async def paper_tick() -> None:
    maker = _get_session_maker()
    async with maker() as session:
        accounts = (
            await session.scalars(
                select(PaperAccount)
                .where(
                    PaperAccount.enabled.is_(True)
                    | PaperAccount.account_id.in_(select(PaperCycle.account_id).where(PaperCycle.status == "pending")),
                )
                .order_by(PaperAccount.next_due.asc().nulls_first())
                .limit(100)
            )
        ).all()
    for account in accounts:
        async with maker() as session:
            pending = await session.scalar(
                select(PaperCycle.cycle_id).where(
                    PaperCycle.account_id == account.account_id, PaperCycle.status == "pending"
                )
            )
        if (
            account.enabled
            and account.refresh_esi
            and pending is None
            and (account.next_due is None or account.next_due <= datetime.now(UTC))
        ):
            try:
                await refresh_market(account.account_id, User(identity=account.user_id, permissions=["SuperAdmin"]))
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                logger.warning(
                    "ESI refresh failed; missing or expired quotes cannot fill paper trades",
                    account_id=account.account_id,
                )
        try:
            await tick_account(account.account_id)
        except (RedisError, OSError):
            logger.exception("Paper run delivery failed; same run ID will be retried", account_id=account.account_id)
