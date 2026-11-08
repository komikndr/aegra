"""Shared atomic journal and existing-executor task creation."""

from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Run, Thread
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmTask
from aegra_api.models.auth import User
from aegra_api.models.run_job import RunExecution, RunIdentity, RunJob


def append_entry(session: AsyncSession, objective: SwarmObjective, *, kind: str, author: str,
                 content: dict[str, Any], review_id: str | None = None) -> None:
    """Caller holds the objective lock; journal and task state commit together."""
    objective.sequence += 1
    session.add(SwarmEntry(objective_id=objective.objective_id, sequence=objective.sequence,
                           review_id=review_id, kind=kind, author=author, content=content))


async def persist_task(session: AsyncSession, objective: SwarmObjective, review: SwarmReview, *,
                       role: str, graph_id: str, inputs: dict[str, Any]) -> SwarmTask:
    task_id, run_id, thread_id = str(uuid4()), str(uuid4()), str(uuid4())
    job = RunJob(identity=RunIdentity(run_id=run_id, thread_id=thread_id, graph_id=graph_id),
                 user=User(identity=objective.user_id, permissions=["SuperAdmin"]),
                 execution=RunExecution(input_data=inputs, config={"recursion_limit": 8}, stream_mode="values"),
                 run_metadata={"swarm_objective_id": objective.objective_id, "swarm_task_id": task_id})
    session.add(Thread(thread_id=thread_id, user_id=objective.user_id, status="idle",
                       metadata_json={"swarm_objective_id": objective.objective_id}))
    await session.flush()
    session.add(Run(run_id=run_id, thread_id=thread_id, assistant_id=None, status="pending", user_id=objective.user_id,
                    input=inputs, config=job.execution.config, context={}, execution_params=job.to_execution_params()))
    await session.flush()
    task = SwarmTask(task_id=task_id, review_id=review.review_id, role=role, status="pending", run_id=run_id)
    session.add(task)
    append_entry(session, objective, kind="task_created", author="onyx:coordinator", review_id=review.review_id,
                 content={"task_id": task_id, "role": role, "run_id": run_id})
    return task
