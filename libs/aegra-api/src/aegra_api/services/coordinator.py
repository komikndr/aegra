"""Owner-scoped conversation, durable dispatch, and transactional action receipts."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.active_runs import active_runs
from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.orm import Run, Thread, _get_session_maker
from aegra_api.core.paper_orm import PaperAccount, PaperLedger
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor
from aegra_api.models.auth import User
from aegra_api.models.coordinator import CoordinatorAnswer, CoordinatorMessage
from aegra_api.models.paper_trading import PaperLoopStart
from aegra_api.models.run_job import RunExecution, RunIdentity, RunJob
from aegra_api.models.swarm_coordination import Feedback, ReviewRequest
from aegra_api.services.broker import broker_manager
from aegra_api.services.executor import executor
from aegra_api.services.paper_loop import configure_loop
from aegra_api.services.swarm_acquisition import approved_markets
from aegra_api.services.swarm_coordination import TERMINAL_RUNS, append_entry, set_objective_status, start_review
from aegra_api.settings import settings


def turn_view(turn: CoordinatorTurn) -> dict[str, Any]:
    return {"turn_id": turn.turn_id, "message": turn.message, "status": turn.status,
            "result": turn.result, "created_at": turn.created_at, "run_id": turn.run_id}


def excerpt(value: Any, limit: int = 4000) -> dict[str, Any]:
    text = json.dumps(value, default=str)
    return {"text": text[:limit], "truncated": len(text) > limit}


async def conversation_snapshot(session: AsyncSession, objective: SwarmObjective) -> dict[str, Any]:
    try:
        markets = approved_markets()
    except ValueError:
        markets = {}
    supervisor = await session.get(SwarmSupervisor, objective.objective_id)
    entries = (await session.scalars(select(SwarmEntry).where(
        SwarmEntry.objective_id == objective.objective_id,
        SwarmEntry.kind.not_in(["coordinator_message", "coordinator_reply"]),
    ).order_by(SwarmEntry.sequence.desc()).limit(8))).all()
    assessment = await session.scalar(select(SwarmEntry).where(
        SwarmEntry.objective_id == objective.objective_id, SwarmEntry.kind == "assessment",
    ).order_by(SwarmEntry.sequence.desc()).limit(1))
    turns = (await session.scalars(select(CoordinatorTurn).where(
        CoordinatorTurn.objective_id == objective.objective_id,
    ).order_by(CoordinatorTurn.created_at.desc(), CoordinatorTurn.turn_id.desc()).limit(6))).all()
    accounts = (await session.scalars(select(PaperAccount).where(
        PaperAccount.user_id == objective.user_id, PaperAccount.policy["objective_id"].as_string() == objective.objective_id,
    ).order_by(PaperAccount.account_id).limit(10))).all()
    portfolios = []
    for account in accounts:
        ledger = await session.scalar(select(PaperLedger).where(PaperLedger.account_id == account.account_id)
                                      .order_by(PaperLedger.created_at.desc()).limit(1))
        portfolios.append({"account_id": account.account_id, "policy": account.policy, "cash_isk": str(account.cash),
                           "positions": account.positions, "enabled": account.enabled, "remaining_cycles": account.remaining,
                           "last_outcome": ledger.entry if ledger else None, "simulation": True})
    return {"objective": {"objective_id": objective.objective_id, **objective.definition, "evidence": None,
                          "status": objective.status, "revision": objective.revision},
            "evidence": [excerpt(item) for item in objective.current_evidence[-8:]],
            "evidence_total": len(objective.current_evidence),
            "recent_board": [{"sequence": item.sequence, "kind": item.kind, "content": excerpt(item.content)} for item in reversed(entries)],
            "latest_assessment": excerpt(assessment.content, 12000) if assessment else None,
            "conversation": [{"user": item.message, "reply": excerpt(item.result)} for item in reversed(turns)],
            "paper_accounts": portfolios,
            "approved_market_scopes": markets,
            "supervisor": {"enabled": supervisor.enabled, "state": supervisor.state, "reason": supervisor.reason,
                           "next_wakeup": supervisor.next_wakeup.isoformat() if supervisor.next_wakeup else None,
                           "deadline": supervisor.deadline.isoformat(), "policy": supervisor.policy,
                           "reviews_started": supervisor.reviews_started,
                           "model_calls_reserved": supervisor.model_calls_reserved,
                           "specialist_tasks_reserved": supervisor.specialist_tasks_reserved} if supervisor else {"state": "unconfigured"},
            "snapshot_at": datetime.now(UTC).isoformat()}


async def create_turn(session: AsyncSession, objective: SwarmObjective, user: User, request: CoordinatorMessage) -> CoordinatorTurn:
    """Caller holds the objective lock. No inference or queue delivery in this transaction."""
    existing = await session.scalar(select(CoordinatorTurn).where(
        CoordinatorTurn.objective_id == objective.objective_id, CoordinatorTurn.request_key == request.request_key,
    ))
    if existing:
        if existing.message != request.message:
            raise HTTPException(409, "Request key already identifies a different message.")
        return existing
    pending = await session.scalar(select(CoordinatorTurn.turn_id).where(
        CoordinatorTurn.objective_id == objective.objective_id, CoordinatorTurn.status == "pending",
    ))
    if pending:
        raise HTTPException(409, "Coordinator is still answering the previous message.")
    snapshot = await conversation_snapshot(session, objective)
    if len(json.dumps(snapshot).encode()) > 150000:
        raise HTTPException(422, "Objective context exceeds the Coordinator budget; narrow the evidence.")
    turn_id, thread_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    job = RunJob(identity=RunIdentity(run_id=run_id, thread_id=thread_id, graph_id="coordinator"),
                 user=User(identity=user.identity, permissions=["SuperAdmin"]),
                 execution=RunExecution(input_data={"message": request.message, "snapshot": snapshot},
                                        config={"recursion_limit": 8}, stream_mode="values"),
                 run_metadata={"swarm_objective_id": objective.objective_id, "coordinator_turn_id": turn_id})
    session.add(Thread(thread_id=thread_id, user_id=user.identity, status="idle",
                       metadata_json={"swarm_objective_id": objective.objective_id, "agent": "coordinator"}))
    await session.flush()
    session.add(Run(run_id=run_id, thread_id=thread_id, user_id=user.identity, status="pending",
                    input=job.execution.input_data, config=job.execution.config, context={}, execution_params=job.to_execution_params()))
    await session.flush()
    turn = CoordinatorTurn(turn_id=turn_id, objective_id=objective.objective_id, request_key=request.request_key,
                           message=request.message, run_id=run_id, status="pending", deadline=datetime.now(UTC) + timedelta(minutes=4))
    session.add(turn)
    append_entry(session, objective, kind="coordinator_message", author=user.identity,
                 content={"turn_id": turn_id, "message": request.message})
    return turn


async def apply_action(
    session: AsyncSession, objective: SwarmObjective, turn: CoordinatorTurn, answer: CoordinatorAnswer, account: PaperAccount | None,
) -> dict[str, Any]:
    user = User(identity=objective.user_id, permissions=["SuperAdmin"])
    key = f"coordinator:{turn.turn_id}"
    if answer.action == "none":
        return {"status": "not_requested"}
    if answer.action in {"review", "strategy_review", "feedback"}:
        request = Feedback(request_key=key, kind=answer.feedback_kind, content=turn.message) if answer.action == "feedback" else ReviewRequest(
            request_key=key, workflow="strategy_search_v1" if answer.action == "strategy_review" else
            "planned_review_v1" if answer.allow_market_acquisition else "evidence_review_v1",
            allow_market_acquisition=answer.allow_market_acquisition)
        review = await start_review(session, objective, user, request)
        return {"status": "applied", "action": answer.action, "review_id": review.review_id, "revision": review.revision}
    if answer.action in {"pause", "resume"}:
        await set_objective_status(session, objective, "paused" if answer.action == "pause" else "active", user)
        return {"status": "applied", "action": answer.action, "objective_status": objective.status}
    if account is None or account.user_id != objective.user_id or account.policy.get("objective_id") != objective.objective_id:
        raise HTTPException(404, "Paper account is not linked to this objective.")
    if answer.action == "paper_stop":
        account.enabled, account.remaining = False, 0
    else:
        if objective.status != "active":
            raise HTTPException(409, "Resume the objective before starting paper execution.")
        await configure_loop(session, account, PaperLoopStart(request_key=key, cycles=answer.cycles,
                                                             interval_seconds=answer.interval_seconds, refresh_esi=True))
    receipt = {"status": "applied", "action": answer.action, "account_id": account.account_id,
               "remaining_cycles": account.remaining, "simulation": True}
    append_entry(session, objective, kind="paper_control", author="onyx:coordinator", content=receipt)
    return receipt


async def tick_turn(turn_id: str) -> None:
    job: RunJob | None = None
    cancel: str | None = None
    async with _get_session_maker()() as session, session.begin():
        turn = await session.get(CoordinatorTurn, turn_id)
        if turn is None or turn.status != "pending":
            return
        run = await session.get(Run, turn.run_id)
        if run is None:
            return
        answer = None
        if run.status == "success":
            try:
                answer = CoordinatorAnswer.model_validate((run.output or {}).get("answer"))
            except ValueError:
                answer = None
        # Paper execution also locks account -> objective; preserve this order.
        account = None
        if answer and answer.action in {"paper_start", "paper_stop"}:
            account = await session.scalar(select(PaperAccount).where(
                PaperAccount.account_id == answer.account_id, PaperAccount.user_id == run.user_id,
                PaperAccount.policy["objective_id"].as_string() == turn.objective_id,
            ).with_for_update())
        objective = await session.scalar(select(SwarmObjective).where(
            SwarmObjective.objective_id == turn.objective_id,
        ).with_for_update(skip_locked=True))
        if objective is None:
            return
        await session.refresh(turn)
        if turn.status != "pending":
            return
        if turn.deadline <= datetime.now(UTC):
            turn.status, turn.result = "failed", {"error": "Coordinator deadline expired; no action was applied."}
            if run.status not in TERMINAL_RUNS:
                cancel = run.run_id
                if run.status == "pending":
                    run.status = "interrupted"
        elif run.status in TERMINAL_RUNS:
            if answer is None:
                turn.status, turn.result = "failed", {"error": "Coordinator could not produce a valid reply; no action was applied."}
            else:
                try:
                    async with session.begin_nested():
                        receipt = await apply_action(session, objective, turn, answer, account)
                except HTTPException as error:
                    receipt = {"status": "refused", "action": answer.action, "message": error.detail}
                    await session.refresh(objective)
                turn.status = "completed"
                turn.result = {"text": answer.text, "receipt": receipt, "model": (run.output or {}).get("model")}
        elif run.status == "pending" and (turn.submitted_at is None or turn.submitted_at < datetime.now(UTC) - timedelta(seconds=30)) and (
            settings.redis.REDIS_BROKER_ENABLED or run.run_id not in active_runs
        ):
            turn.submitted_at = datetime.now(UTC)
            job = RunJob.from_run_orm(run)
        if turn.status != "pending":
            append_entry(session, objective, kind="coordinator_reply", author="onyx:coordinator",
                         content={"turn_id": turn.turn_id, **(turn.result or {})})
    if cancel:
        await broker_manager.request_cancel(cancel, "cancel")
    if job:
        await executor.submit(job)
