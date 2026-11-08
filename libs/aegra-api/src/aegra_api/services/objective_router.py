"""Head Coordinator access uses the same role and ownership policy as objective APIs."""

import hashlib
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.operations_access import require_operations_access
from aegra_api.core.orm import _get_session_maker
from aegra_api.core.swarm_orm import SwarmObjective
from aegra_api.models.auth import User
from aegra_api.models.coordinator import CoordinatorMessage
from aegra_api.services.coordinator import conversation_snapshot, create_turn, excerpt, turn_view
from aegra_api.services.swarm_coordination import owned_objective


async def discover_objectives(user: User, query: str = "", offset: int = 0) -> dict[str, Any]:
    """Search only the caller's objectives. Never expose another owner's existence."""
    require_operations_access(user)
    offset = max(0, min(offset, 10000))
    query = query.strip()[:200]
    async with _get_session_maker()() as session:
        statement = select(SwarmObjective).where(SwarmObjective.user_id == user.identity)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(SwarmObjective.definition["title"].as_string().ilike(f"%{escaped}%", escape="\\"))
        rows = (await session.scalars(statement.order_by(SwarmObjective.created_at.desc(), SwarmObjective.objective_id)
                                     .offset(offset).limit(21))).all()
        return {"objectives": [{"objective_id": row.objective_id, "title": row.definition["title"],
                                "goal": row.definition["goal"][:800], "status": row.status, "revision": row.revision,
                                "workspace": f"/control-plane/objectives/{row.objective_id}"} for row in rows[:20]],
                "next_offset": offset + 20 if len(rows) > 20 else None}


async def inspect_objective(user: User, objective_id: str) -> dict[str, Any]:
    require_operations_access(user)
    async with _get_session_maker()() as session:
        objective = await owned_objective(session, objective_id, user)
        return {"objective_id": objective.objective_id, "workspace": f"/control-plane/objectives/{objective.objective_id}",
                "context": excerpt(await conversation_snapshot(session, objective), 30000)}


async def delegate_message(user: User, objective_id: str, message: str, call_id: str) -> dict[str, Any]:
    """Persist an exact user message, not a model-authored instruction. Retry the same call safely."""
    require_operations_access(user)
    key = hashlib.sha256(f"{user.identity}:{objective_id}:{call_id}".encode()).hexdigest()
    request = CoordinatorMessage(request_key=f"head:{key}", message=message)
    async with _get_session_maker()() as session:
        objective = await owned_objective(session, objective_id, user, lock=True)
        turn = await create_turn(session, objective, user, request)
        await session.commit()
        await session.refresh(turn)
        return {**turn_view(turn), "objective_id": objective_id, "workspace": f"/control-plane/objectives/{objective_id}"}


async def inspect_reply(user: User, objective_id: str, turn_id: str) -> dict[str, Any]:
    require_operations_access(user)
    async with _get_session_maker()() as session:
        await owned_objective(session, objective_id, user)
        turn = await session.scalar(select(CoordinatorTurn).where(
            CoordinatorTurn.objective_id == objective_id, CoordinatorTurn.turn_id == turn_id,
        ))
        if turn is None:
            raise HTTPException(404, "Conversation turn not found.")
        return {**turn_view(turn), "objective_id": objective_id, "workspace": f"/control-plane/objectives/{objective_id}"}
