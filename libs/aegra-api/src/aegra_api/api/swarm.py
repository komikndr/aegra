"""Control-plane routing, durable objectives, and evidence-review interfaces."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.operations_access import require_operations_access
from aegra_api.core.orm import get_session
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmStrategyVersion, SwarmTask
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor
from aegra_api.models.auth import User
from aegra_api.models.coordinator import CoordinatorMessage
from aegra_api.models.swarm import RoutingResult, RoutingScenario, Signal
from aegra_api.models.swarm_coordination import EntryView, Feedback, ObjectiveCreate, ObjectiveView, ReviewRequest
from aegra_api.models.swarm_supervisor import SupervisorCommand, SupervisorStart
from aegra_api.services.coordinator import create_turn, turn_view
from aegra_api.services.swarm_acquisition import approved_markets
from aegra_api.services.swarm_capabilities import CAPABILITIES
from aegra_api.services.swarm_coordination import (
    create_objective,
    objective_view,
    owned_objective,
    set_objective_status,
    start_review,
)
from aegra_api.services.swarm_scenarios import routing_scenarios
from aegra_api.services.swarm_supervisor import command_supervisor, configure_supervisor, supervisor_view
from aegra_api.services.systemone import SystemOneSettings, route_signal

router = APIRouter(prefix="/swarm/v1", tags=["Swarm"], dependencies=[Depends(require_operations_access)])


@router.get("/capabilities")
def capabilities() -> dict[str, object]:
    try:
        settings = SystemOneSettings.from_environment()
        markets = approved_markets()
    except ValueError:
        raise HTTPException(503, "Invalid server routing or evidence configuration.") from None
    return {
        "systemone": {
            "role": "signal_router",
            "model": settings.model or None,
            "transport": "typesafe_systemone",
            "configured": settings.configured,
        },
        "routing_destinations": ["local_assessment", "strategic_review", "record_only"],
        "routing_dispatch": False,
        "durable_coordination": True,
        "conversation_agent": "Coordinator",
        "objective_supervisor": {"available": True, "workflow": "evidence_review_v1", "model_led_planning": True,
                                 "max_reviews_per_start": 24, "execution_actions": False},
        "workflows": ["evidence_review_v1", "planned_review_v1", "strategy_search_v1", "paper_trade_v1"],
        "strategic_search": {"max_branches": 2, "max_passes": 2, "max_model_calls": 11,
                             "specialist_reservation": 8, "verification": ["stock_calculation", "evidence_freshness"],
                             "sandbox": False, "execution_actions": False},
        "planning": {"capabilities": CAPABILITIES, "max_tasks": 2, "max_model_calls_per_review": 4,
                     "external_retrieval": bool(markets), "execution_actions": False,
                     "market_acquisition": {"scopes": markets, "opt_in_required": True, "max_per_review": 1}},
        "paper_trading": {"simulation_only": True, "max_cycles_per_start": 24, "real_orders": False},
        "execution": "existing_aegra_executor",
        "external_execution": False,
        "scenario_runner": False,
        "message": "Configuration is not a health check. Routing does not dispatch work. Objective reviews use an explicit approved workflow.",
    }


@router.post("/routing/assess", response_model=RoutingResult)
async def assess_routing(signal: Signal) -> RoutingResult:
    try:
        return await route_signal(signal)
    except ValueError:
        raise HTTPException(503, "Invalid server SystemOne configuration.") from None


@router.get("/simulations/routing", response_model=list[RoutingScenario])
def scenarios() -> list[RoutingScenario]:
    return list(routing_scenarios())


@router.post("/objectives", response_model=ObjectiveView)
async def post_objective(
    request: ObjectiveCreate,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await create_objective(session, user, request)
    await session.commit()
    await session.refresh(objective)
    return objective_view(objective)


@router.get("/objectives", response_model=list[ObjectiveView])
async def list_objectives(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10_000),
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(SwarmObjective).where(
        SwarmObjective.user_id == user.identity,
    ).order_by(SwarmObjective.created_at.desc(), SwarmObjective.objective_id).offset(offset).limit(limit))).all()
    return await objective_summaries(session, list(rows))


async def objective_summaries(session: AsyncSession, rows: list[SwarmObjective]) -> list[dict[str, Any]]:
    ids = [row.objective_id for row in rows]
    assessments = (await session.scalars(select(SwarmEntry).where(
        SwarmEntry.objective_id.in_(ids), SwarmEntry.kind == "assessment",
    ).order_by(SwarmEntry.objective_id, SwarmEntry.sequence.desc()).distinct(SwarmEntry.objective_id))).all()
    reviews = (await session.scalars(select(SwarmReview).where(SwarmReview.objective_id.in_(ids))
                                    .order_by(SwarmReview.objective_id, SwarmReview.revision.desc()).distinct(SwarmReview.objective_id))).all()
    assessment_map = {item.objective_id: item.content for item in assessments}
    review_map = {item.objective_id: item.status for item in reviews}
    supervisors = (await session.scalars(select(SwarmSupervisor).where(SwarmSupervisor.objective_id.in_(ids)))).all()
    supervisor_map = {item.objective_id: supervisor_view(item) for item in supervisors}
    return [{**objective_view(row), "latest_assessment": assessment_map.get(row.objective_id),
             "latest_review_status": review_map.get(row.objective_id),
             "supervisor": supervisor_map.get(row.objective_id)} for row in rows]


@router.get("/objectives/{objective_id}", response_model=ObjectiveView)
async def get_objective(
    objective_id: str, user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return (await objective_summaries(session, [await owned_objective(session, objective_id, user)]))[0]


@router.get("/objectives/{objective_id}/evidence")
async def get_evidence(
    objective_id: str, user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    return (await owned_objective(session, objective_id, user)).current_evidence


@router.get("/objectives/{objective_id}/supervisor")
async def get_supervisor(
    objective_id: str, user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await owned_objective(session, objective_id, user)
    row = await session.get(SwarmSupervisor, objective_id)
    return supervisor_view(row) if row is not None else {"objective_id": objective_id, "enabled": False, "state": "unconfigured"}


@router.post("/objectives/{objective_id}/supervisor/start")
async def start_supervisor(
    objective_id: str, request: SupervisorStart,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    row = await configure_supervisor(session, objective, request)
    await session.commit()
    return supervisor_view(row)


@router.post("/objectives/{objective_id}/supervisor/{command}")
async def supervisor_command(
    objective_id: str, command: Literal["wake", "stop"], request: SupervisorCommand,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    row = await command_supervisor(session, objective, request, stop=command == "stop")
    await session.commit()
    return supervisor_view(row)


@router.post("/objectives/{objective_id}/conversation")
async def post_conversation(
    objective_id: str, request: CoordinatorMessage,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    turn = await create_turn(session, objective, user, request)
    await session.commit()
    await session.refresh(turn)
    return turn_view(turn)


@router.get("/objectives/{objective_id}/conversation")
async def get_conversation(
    objective_id: str, limit: int = Query(50, ge=1, le=100),
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_objective(session, objective_id, user)
    turns = (await session.scalars(select(CoordinatorTurn).where(CoordinatorTurn.objective_id == objective_id)
                                  .order_by(CoordinatorTurn.created_at.desc(), CoordinatorTurn.turn_id.desc()).limit(limit))).all()
    return [turn_view(turn) for turn in reversed(turns)]


@router.post("/objectives/{objective_id}/status", response_model=ObjectiveView)
async def objective_status(
    objective_id: str,
    status: Literal["active", "paused"] = Query(...),
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    await set_objective_status(session, objective, status, user)
    await session.commit()
    return objective_view(objective)


def review_view(review: SwarmReview) -> dict[str, Any]:
    return {"review_id": review.review_id, "revision": review.revision, "status": review.status, "deadline": review.deadline,
            "workflow": review.request.get("workflow", "evidence_review_v1")}


@router.post("/objectives/{objective_id}/reviews")
async def post_review(
    objective_id: str,
    request: ReviewRequest,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    review = await start_review(session, objective, user, request)
    await session.commit()
    return review_view(review)


@router.post("/objectives/{objective_id}/feedback")
async def post_feedback(
    objective_id: str,
    request: Feedback,
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    objective = await owned_objective(session, objective_id, user, lock=True)
    review = await start_review(session, objective, user, request)
    await session.commit()
    return review_view(review)


@router.get("/objectives/{objective_id}/reviews")
async def list_reviews(
    objective_id: str,
    *,
    limit: int = Query(20, ge=1, le=100),
    before_revision: int | None = Query(None, ge=1),
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_objective(session, objective_id, user)
    query = select(SwarmReview).where(SwarmReview.objective_id == objective_id)
    if before_revision is not None:
        query = query.where(SwarmReview.revision < before_revision)
    reviews = (await session.scalars(query.order_by(SwarmReview.revision.desc()).limit(limit))).all()
    return [review_view(review) for review in reviews]


@router.get("/objectives/{objective_id}/reviews/{review_id}")
async def get_review(
    objective_id: str, review_id: str,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await owned_objective(session, objective_id, user)
    review = await session.scalar(select(SwarmReview).where(
        SwarmReview.review_id == review_id, SwarmReview.objective_id == objective_id,
    ))
    if review is None:
        raise HTTPException(404, "Review not found.")
    versions = list((await session.scalars(select(SwarmStrategyVersion).where(
        SwarmStrategyVersion.review_id == review.review_id,
    ).order_by(SwarmStrategyVersion.version, SwarmStrategyVersion.branch_id))).all())
    return {**review_view(review), "input_snapshot": review.snapshot, "plan": review.plan, "acquisition": review.acquisition,
            "strategy_versions": [{"branch_id": v.branch_id, "version": v.version, "content": v.content,
                                   "created_at": v.created_at} for v in versions]}


@router.get("/objectives/{objective_id}/board", response_model=list[EntryView])
async def board_entries(
    objective_id: str,
    *,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    latest: bool = Query(False),
    user: User = Depends(require_operations_access),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_objective(session, objective_id, user)
    rows = (await session.scalars(select(SwarmEntry).where(
        SwarmEntry.objective_id == objective_id, SwarmEntry.sequence > after,
    ).order_by(SwarmEntry.sequence.desc() if latest else SwarmEntry.sequence).limit(limit))).all()
    if latest:
        rows = list(reversed(rows))
    return [{"sequence": row.sequence, "review_id": row.review_id, "kind": row.kind, "author": row.author,
             "content": row.content, "created_at": row.created_at} for row in rows]


@router.get("/objectives/{objective_id}/reviews/{review_id}/tasks")
async def list_tasks(
    objective_id: str, review_id: str,
    user: User = Depends(require_operations_access), session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await owned_objective(session, objective_id, user)
    review = await session.scalar(select(SwarmReview).where(
        SwarmReview.review_id == review_id, SwarmReview.objective_id == objective_id,
    ))
    if review is None:
        raise HTTPException(404, "Review not found.")
    tasks = (await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review_id).order_by(SwarmTask.role))).all()
    return [{"task_id": task.task_id, "role": task.role, "status": task.status, "run_id": task.run_id,
             "result": task.result} for task in tasks]
