"""Recoverable dispatch/reconciliation, backed by existing Aegra runs and workers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import FastAPI
from redis import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from aegra_api.core.active_runs import active_runs
from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.orm import Run, _get_session_maker
from aegra_api.core.paper_orm import PaperCycle
from aegra_api.core.swarm_orm import SwarmObjective, SwarmReview, SwarmTask
from aegra_api.models.run_job import RunJob
from aegra_api.services.broker import broker_manager
from aegra_api.services.coordinator import tick_turn
from aegra_api.services.executor import executor
from aegra_api.services.paper_loop import paper_tick
from aegra_api.services.swarm_coordination import TERMINAL_RUNS, advance_review
from aegra_api.services.swarm_supervisor import supervisor_tick
from aegra_api.settings import settings

logger = structlog.get_logger(__name__)


async def coordination_tick() -> None:
    await supervisor_tick()
    maker = _get_session_maker()
    async with maker() as session:
        turn_ids = list((await session.scalars(select(CoordinatorTurn.turn_id).where(
            CoordinatorTurn.status == "pending",
        ).order_by(CoordinatorTurn.created_at).limit(100))).all())
    for turn_id in turn_ids:
        try:
            await tick_turn(turn_id)
        except (RedisError, OSError):
            logger.exception("Coordinator delivery failed; durable intent will be retried", turn_id=turn_id)
    async with maker() as session:
        review_ids = list((await session.scalars(select(SwarmReview.review_id).where(
            SwarmReview.status == "running",
        ).order_by(SwarmReview.deadline).limit(100))).all())
    for review_id in review_ids:
        async with maker() as session, session.begin():
            review = await session.get(SwarmReview, review_id)
            if review is None:
                continue
            objective = await session.scalar(select(SwarmObjective).where(
                SwarmObjective.objective_id == review.objective_id,
            ).with_for_update(skip_locked=True))
            if objective is not None:
                # Refresh after taking the objective lock: feedback or pause might
                # have superseded this review while this transaction waited.
                await session.refresh(review)
                await advance_review(session, objective, review)
    async with maker() as session:
        task_ids = list((await session.scalars(select(SwarmTask.task_id).where(
            SwarmTask.status == "pending",
        ).order_by(SwarmTask.last_submitted_at.asc().nulls_first(), SwarmTask.task_id).limit(100))).all())
    for task_id in task_ids:
        try:
            await dispatch_task(task_id)
        except (RedisError, OSError):
            logger.exception("Swarm task delivery failed; intent will be retried", task_id=task_id)


async def dispatch_task(task_id: str) -> None:
    maker = _get_session_maker()
    job: RunJob | None = None
    cancel_run: str | None = None
    async with maker() as session, session.begin():
        task = await session.get(SwarmTask, task_id)
        if task is None:
            return
        review = await session.get(SwarmReview, task.review_id)
        if review is None:
            return
        objective = await session.scalar(select(SwarmObjective).where(
            SwarmObjective.objective_id == review.objective_id,
        ).with_for_update(skip_locked=True))
        if objective is None:
            return
        await session.refresh(review)
        await session.refresh(task)
        run = await session.get(Run, task.run_id)
        if task.status != "pending" or run is None:
            return
        if run.status in TERMINAL_RUNS:
            if review.status != "running":
                task.status = "cancelled"
            return
        if review.status != "running" or objective.status != "active":
            if run.status == "pending":
                run.status, task.status = "interrupted", "cancelled"
            else:
                cancel_run = task.run_id
        elif run.status == "pending" and (
            task.last_submitted_at is None or task.last_submitted_at < datetime.now(UTC) - timedelta(seconds=30)
        ) and (settings.redis.REDIS_BROKER_ENABLED or task.run_id not in active_runs):
            # Commit intent timestamp before network delivery. A crash in this gap
            # retries the SAME run ID; Redis workers fence duplicate deliveries.
            task.last_submitted_at = datetime.now(UTC)
            job = RunJob.from_run_orm(run)
    if cancel_run is not None:
        await broker_manager.request_cancel(cancel_run, "cancel")
    if job is not None:
        await executor.submit(job)


async def recover_local_runs() -> None:
    """Single-instance dev recovery only; production uses the existing lease reaper."""
    if settings.redis.REDIS_BROKER_ENABLED:
        return
    maker = _get_session_maker()
    async with maker() as session, session.begin():
        runs = (await session.scalars(select(Run).where(
            Run.run_id.in_(select(SwarmTask.run_id)) | Run.run_id.in_(select(PaperCycle.run_id)) | Run.run_id.in_(select(CoordinatorTurn.run_id)),
            Run.status.in_(["running", "pending"]),
        ))).all()
        for run in runs:
            run.status = "pending"
        tasks = (await session.scalars(select(SwarmTask).where(SwarmTask.status == "pending"))).all()
        for task in tasks:
            task.last_submitted_at = None
        cycles = (await session.scalars(select(PaperCycle).where(PaperCycle.status == "pending"))).all()
        for cycle in cycles:
            cycle.submitted_at = None
        turns = (await session.scalars(select(CoordinatorTurn).where(CoordinatorTurn.status == "pending"))).all()
        for turn in turns:
            turn.submitted_at = None


async def coordination_loop() -> None:
    while True:
        try:
            await coordination_tick()
        except (SQLAlchemyError, RedisError, OSError, ValueError):
            logger.exception("Swarm coordination tick failed; durable intents remain pending")
        await asyncio.sleep(2)


async def paper_loop() -> None:
    while True:
        try:
            await paper_tick()
        except (SQLAlchemyError, RedisError, OSError, ValueError):
            logger.exception("Paper cycle tick failed; durable intents remain pending")
        await asyncio.sleep(2)


@asynccontextmanager
async def swarm_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await recover_local_runs()
    task = asyncio.create_task(coordination_loop(), name="swarm-coordination")
    paper_task = asyncio.create_task(paper_loop(), name="paper-trading")
    try:
        yield
    finally:
        task.cancel()
        paper_task.cancel()
        await asyncio.gather(task, paper_task, return_exceptions=True)
