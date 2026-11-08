"""Finite scheduled reviews under the existing objective lock and worker lifecycle.

All transitions below require the objective lock. No model or queue call occurs
inside these transactions. Budgets cover supervisor-created reviews only.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import _get_session_maker
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor, SwarmSupervisorCommand
from aegra_api.models.auth import User
from aegra_api.models.swarm_coordination import ReviewRequest
from aegra_api.models.swarm_supervisor import SupervisorCommand, SupervisorStart
from aegra_api.services.swarm_acquisition import approved_markets
from aegra_api.services.swarm_coordination import advance_review, append_entry, start_review

logger = structlog.get_logger(__name__)


def evidence_fingerprint(objective: SwarmObjective) -> str:
    ordered = sorted(objective.current_evidence, key=lambda item: item["evidence_id"])
    return hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest()


def supervisor_view(row: SwarmSupervisor) -> dict[str, Any]:
    return {key: getattr(row, key) for key in (
        "objective_id", "generation", "policy", "enabled", "state", "reason", "next_wakeup", "deadline",
        "reviews_started", "model_calls_reserved", "specialist_tasks_reserved", "review_id", "owns_review", "wake_pending",
    )}


async def duplicate_command(session: AsyncSession, objective: SwarmObjective, key: str, payload: dict[str, Any]) -> bool:
    existing = await session.get(SwarmSupervisorCommand, (objective.objective_id, key))
    if existing is not None:
        if existing.payload != payload:
            raise HTTPException(409, "The request key already identifies a different supervisor command.")
        return True
    session.add(SwarmSupervisorCommand(objective_id=objective.objective_id, request_key=key, payload=payload))
    return False


def transition(session: AsyncSession, objective: SwarmObjective, row: SwarmSupervisor, state: str, reason: str) -> None:
    row.state, row.reason = state, reason
    append_entry(session, objective, kind="supervisor_state", author="onyx:supervisor", content={
        "generation": row.generation, "state": state, "reason": reason,
        "review_id": row.review_id, "reviews_started": row.reviews_started,
        "model_calls_reserved": row.model_calls_reserved, "specialist_tasks_reserved": row.specialist_tasks_reserved,
        "next_wakeup": row.next_wakeup.isoformat() if row.next_wakeup else None,
    })


async def stop_supervisor(
    session: AsyncSession, objective: SwarmObjective, row: SwarmSupervisor, reason: str,
) -> None:
    row.enabled, row.next_wakeup, row.wake_pending = False, None, False
    if row.owns_review and row.review_id:
        review = await session.get(SwarmReview, row.review_id)
        if review is not None and review.status == "running":
            review.status = "cancelled"
            append_entry(session, objective, kind="review_finished", author="onyx:supervisor",
                         review_id=review.review_id, content={"status": "cancelled", "reason": reason})
    transition(session, objective, row, "stopped", reason)


async def configure_supervisor(
    session: AsyncSession, objective: SwarmObjective, request: SupervisorStart,
) -> SwarmSupervisor:
    payload = {"command": "start", **request.model_dump(exclude={"request_key"})}
    if not request.allow_market_acquisition:
        payload.pop("allow_market_acquisition", None)
    elif request.workflow not in {"planned_review_v1", "strategy_search_v1"}:
        raise HTTPException(422, "Market acquisition requires a planned or strategy-search review.")
    else:
        try:
            scopes = approved_markets()
        except ValueError:
            raise HTTPException(503, "Invalid server EVE market configuration.") from None
        if not scopes:
            raise HTTPException(422, "No server-approved EVE markets are configured.")
    if request.workflow == "evidence_review_v1":
        payload.pop("workflow", None)
    duplicate = await duplicate_command(session, objective, request.request_key, payload)
    row = await session.get(SwarmSupervisor, objective.objective_id)
    if duplicate:
        if row is None:
            raise RuntimeError("Supervisor command has no state.")
        return row
    if objective.status != "active":
        raise HTTPException(409, "Resume the objective before starting supervision.")
    if row is not None and row.enabled:
        raise HTTPException(409, "Stop the active supervisor before changing its policy.")
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "generation": str(uuid4()), "policy": {key: value for key, value in payload.items() if key != "command"}, "enabled": True,
        "state": "scheduled", "reason": "Explicit supervision start.", "next_wakeup": now, "checked_at": None,
        "deadline": now + timedelta(seconds=request.max_elapsed_seconds), "reviews_started": 0,
        "model_calls_reserved": 0, "specialist_tasks_reserved": 0, "review_id": None, "owns_review": False,
        "evidence_fingerprint": evidence_fingerprint(objective), "wake_pending": True,
    }
    if row is None:
        row = SwarmSupervisor(objective_id=objective.objective_id, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    transition(session, objective, row, "scheduled", row.reason)
    return row


async def command_supervisor(
    session: AsyncSession, objective: SwarmObjective, request: SupervisorCommand, *, stop: bool,
) -> SwarmSupervisor:
    row = await session.get(SwarmSupervisor, objective.objective_id)
    if row is None:
        raise HTTPException(404, "Supervisor not configured.")
    payload = {"command": "stop" if stop else "wake", "reason": request.reason}
    if await duplicate_command(session, objective, request.request_key, payload):
        return row
    if stop:
        await stop_supervisor(session, objective, row, request.reason)
    else:
        if not row.enabled or objective.status != "active":
            raise HTTPException(409, "Start supervision on an active objective before requesting a wakeup.")
        # Multiple events collapse into one persisted pending wakeup, not a queue.
        row.wake_pending, row.next_wakeup = True, datetime.now(UTC)
        transition(session, objective, row, "reviewing" if row.review_id else "scheduled", request.reason)
    return row


async def reconcile_supervisor(session: AsyncSession, objective: SwarmObjective, row: SwarmSupervisor) -> None:
    if not row.enabled:
        return
    now = datetime.now(UTC)
    if objective.status != "active" or row.deadline <= now:
        await stop_supervisor(session, objective, row, "Objective paused." if objective.status != "active" else "Elapsed-time budget exhausted.")
        return
    review = await session.get(SwarmReview, row.review_id) if row.review_id else None
    if review is not None and review.status == "running":
        await advance_review(session, objective, review)
        if review.status == "running":
            return
    active = await session.scalar(select(SwarmReview).where(
        SwarmReview.objective_id == objective.objective_id, SwarmReview.status == "running",
    ).order_by(SwarmReview.revision.desc()).limit(1))
    if active is not None:
        # Human requests/feedback already started work. Await it, never duplicate it.
        row.review_id, row.owns_review, row.wake_pending = active.review_id, False, False
        row.evidence_fingerprint, row.next_wakeup = evidence_fingerprint(objective), None
        transition(session, objective, row, "reviewing", "Waiting for the existing objective review.")
        return
    if review is not None:
        row.review_id, row.owns_review = None, False
        if review.status == "completed" and (review.plan or {}).get("decision") == "stop":
            await stop_supervisor(session, objective, row, "Validated plan requested supervision stop.")
            return
        if review.status != "completed":
            row.next_wakeup = None
            transition(session, objective, row, "waiting_for_input", f"Review {review.status}; explicit wakeup or changed evidence required.")
        else:
            entry = await session.scalar(select(SwarmEntry).where(
                SwarmEntry.objective_id == objective.objective_id, SwarmEntry.review_id == review.review_id,
                SwarmEntry.kind == "assessment",
            ).order_by(SwarmEntry.sequence.desc()).limit(1))
            needs_information = entry is None or entry.content["assessment"]["disposition"] == "needs_information"
            row.next_wakeup = None if needs_information else now + timedelta(seconds=row.policy["interval_seconds"])
            transition(session, objective, row, "waiting_for_input" if needs_information else "scheduled",
                       "New information required." if needs_information else "Review completed; interval scheduled.")
        if row.reviews_started >= row.policy["max_reviews"]:
            await stop_supervisor(session, objective, row, "Review budget exhausted.")
            return
    fingerprint = evidence_fingerprint(objective)
    if fingerprint != row.evidence_fingerprint:
        row.wake_pending, row.next_wakeup = True, now
    if not row.wake_pending and (row.next_wakeup is None or row.next_wakeup > now):
        return
    if not objective.current_evidence and not row.policy.get("allow_market_acquisition", False):
        row.wake_pending, row.next_wakeup, row.evidence_fingerprint = False, None, fingerprint
        transition(session, objective, row, "waiting_for_input", "No evidence supplied; waiting without model calls.")
        return
    workflow = row.policy.get("workflow", "evidence_review_v1")
    model_reservation = 11 if workflow == "strategy_search_v1" else 4 if workflow == "planned_review_v1" else 3
    specialist_reservation = 8 if workflow == "strategy_search_v1" else 2
    if (row.reviews_started >= row.policy["max_reviews"]
            or row.model_calls_reserved + model_reservation > row.policy["max_model_calls"]
            or row.specialist_tasks_reserved + specialist_reservation > row.policy["max_specialist_tasks"]):
        await stop_supervisor(session, objective, row, "Review, model-call, or specialist-task budget exhausted.")
        return
    # Creating the review, reserving its worst-case calls, and journaling intent are atomic.
    try:
        async with session.begin_nested():
            review = await start_review(session, objective, User(identity=objective.user_id, permissions=["SuperAdmin"]),
                                        ReviewRequest(request_key=f"supervisor:{row.generation}:{row.reviews_started + 1}", workflow=workflow,
                                                      allow_market_acquisition=row.policy.get("allow_market_acquisition", False)))
    except HTTPException:
        await session.refresh(objective)
        row.wake_pending, row.next_wakeup = False, None
        row.evidence_fingerprint = fingerprint
        transition(session, objective, row, "waiting_for_input", "Review input was refused; revise evidence or request a new wakeup.")
        return
    review.deadline = min(review.deadline, row.deadline)
    row.review_id, row.owns_review, row.wake_pending = review.review_id, True, False
    row.reviews_started += 1
    row.model_calls_reserved += model_reservation
    row.specialist_tasks_reserved += specialist_reservation
    row.evidence_fingerprint, row.next_wakeup = fingerprint, None
    transition(session, objective, row, "reviewing", "Changed evidence or a scheduled/explicit wakeup started a bounded review.")


async def supervisor_tick() -> None:
    maker = _get_session_maker()
    async with maker() as session:
        ids = list((await session.scalars(select(SwarmSupervisor.objective_id).where(SwarmSupervisor.enabled.is_(True))
                                          .order_by(SwarmSupervisor.checked_at.asc().nulls_first(), SwarmSupervisor.objective_id)
                                          .limit(100))).all())
    for objective_id in ids:
        try:
            async with maker() as session, session.begin():
                objective = await session.scalar(select(SwarmObjective).where(SwarmObjective.objective_id == objective_id)
                                                 .with_for_update(skip_locked=True))
                if objective is not None:
                    row = await session.get(SwarmSupervisor, objective_id)
                    if row is not None:
                        row.checked_at = datetime.now(UTC)
                        await reconcile_supervisor(session, objective, row)
        except (SQLAlchemyError, ValueError):
            logger.exception("Supervisor transition failed; durable state retained", objective_id=objective_id)
