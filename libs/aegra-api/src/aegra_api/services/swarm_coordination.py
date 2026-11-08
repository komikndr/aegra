"""Atomic objective transitions for fixed and model-planned bounded reviews.

Task rows are dispatch intents, not a second execution queue. Their RunJob records
are committed in the same transaction and executed by the existing executor.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Run
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmStrategyVersion, SwarmTask
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor
from aegra_api.models.auth import User
from aegra_api.models.swarm_coordination import Assessment, Feedback, ObjectiveCreate, ReviewRequest
from aegra_api.models.swarm_planning import PlannedTask
from aegra_api.services.swarm_acquisition import approved_markets, validate_receipt
from aegra_api.services.swarm_capabilities import expired_evidence, stock_calculation, validate_plan
from aegra_api.services.swarm_strategy import advance_strategy_review
from aegra_api.services.swarm_task_io import append_entry, persist_task

GRAPH_ID = "swarm_review"
SPECIALISTS = ("evidence_analyst", "skeptical_reviewer")
TERMINAL_RUNS = frozenset({"success", "error", "timeout", "interrupted", "cancelled"})


def objective_view(objective: SwarmObjective) -> dict[str, Any]:
    return {
        "objective_id": objective.objective_id,
        **{key: objective.definition[key] for key in ("title", "goal", "success_criteria")},
        "status": objective.status,
        "revision": objective.revision,
        "created_at": objective.created_at,
    }


async def owned_objective(session: AsyncSession, objective_id: str, user: User, *, lock: bool = False) -> SwarmObjective:
    statement = select(SwarmObjective).where(
        SwarmObjective.objective_id == objective_id, SwarmObjective.user_id == user.identity
    )
    if lock:
        statement = statement.with_for_update()
    objective = await session.scalar(statement)
    if objective is None:
        raise HTTPException(404, "Objective not found.")
    return objective


async def create_objective(session: AsyncSession, user: User, request: ObjectiveCreate) -> SwarmObjective:
    definition = request.model_dump(mode="json", exclude={"request_key"})
    objective_id = str(uuid4())
    await session.execute(insert(SwarmObjective).values(
        objective_id=objective_id, user_id=user.identity, request_key=request.request_key,
        definition=definition, current_evidence=definition["evidence"], status="active", revision=0, sequence=0,
    ).on_conflict_do_nothing(index_elements=["user_id", "request_key"]))
    objective = await session.scalar(select(SwarmObjective).where(
        SwarmObjective.user_id == user.identity, SwarmObjective.request_key == request.request_key,
    ).with_for_update())
    if objective is None:
        raise RuntimeError("Objective insert did not resolve.")
    if objective.definition != definition:
        raise HTTPException(409, "The request key already identifies a different objective.")
    if objective.sequence == 0:
        append_entry(session, objective, kind="objective_created", author=user.identity, content=definition)
    return objective


async def review_snapshot(session: AsyncSession, objective: SwarmObjective) -> dict[str, Any]:
    entries = list((await session.scalars(select(SwarmEntry).where(
        SwarmEntry.objective_id == objective.objective_id,
        SwarmEntry.kind.in_(["feedback", "assessment", "blocker", "paper_execution"]),
    ).order_by(SwarmEntry.sequence.desc()).limit(6))).all())
    # Earlier evidence is retained in the journal. Latest supplied records with the
    # same ID supersede their value in this bounded snapshot, never in history.
    evidence = {item["evidence_id"]: item for item in objective.current_evidence}
    if len(evidence) > 40:
        raise HTTPException(422, "Review evidence exceeds 40 records; create a narrower objective.")
    snapshot = {
        "objective": {key: objective.definition[key] for key in ("title", "goal", "success_criteria")},
        "evidence": list(evidence.values()),
        "board": [{"sequence": entry.sequence, "kind": entry.kind, "content": entry.content} for entry in reversed(entries)],
        "snapshot_at": datetime.now(UTC).isoformat(),
    }
    latest_assessment = await session.scalar(select(SwarmEntry).where(
        SwarmEntry.objective_id == objective.objective_id, SwarmEntry.kind == "assessment",
    ).order_by(SwarmEntry.sequence.desc()).limit(1))
    snapshot["previous_assessment"] = latest_assessment.content if latest_assessment is not None else None
    cutoff = datetime.now(UTC)
    if any(datetime.fromisoformat(item["available_at"]) > cutoff for item in evidence.values()):
        raise HTTPException(422, "Evidence is not yet available at the review time.")
    if len(json.dumps(snapshot).encode()) > 128_000:
        raise HTTPException(422, "Review snapshot exceeds 128 KB; supply narrower evidence.")
    return snapshot


async def start_review(
    session: AsyncSession, objective: SwarmObjective, user: User, request: ReviewRequest | Feedback,
) -> SwarmReview:
    payload = request.model_dump(mode="json", exclude={"request_key"})
    if not request.allow_market_acquisition:
        payload.pop("allow_market_acquisition", None)
    if request.allow_market_acquisition and request.workflow not in {"planned_review_v1", "strategy_search_v1"}:
        raise HTTPException(422, "Market acquisition requires a planned or strategy-search review.")
    if request.workflow == "evidence_review_v1":
        payload.pop("workflow", None)  # Preserve idempotency for pre-planner requests.
    existing = await session.scalar(select(SwarmReview).where(
        SwarmReview.objective_id == objective.objective_id, SwarmReview.request_key == request.request_key,
    ))
    if existing is not None:
        if existing.request != payload:
            raise HTTPException(409, "The request key already identifies a different review input.")
        return existing
    if objective.status != "active":
        raise HTTPException(409, "Resume the objective before requesting a review.")
    active = list((await session.scalars(select(SwarmReview).where(
        SwarmReview.objective_id == objective.objective_id, SwarmReview.status == "running",
    ))).all())
    if active and not isinstance(request, Feedback):
        raise HTTPException(409, "An objective review is already running.")
    for old in active:
        old.status = "superseded"
        append_entry(session, objective, kind="review_superseded", author=user.identity,
                     review_id=old.review_id, content={"reason": "New feedback requires a fresh assessment."})
    if isinstance(request, Feedback):
        append_entry(session, objective, kind="feedback", author=user.identity, content=payload)
        current = {item["evidence_id"]: item for item in objective.current_evidence}
        current.update({item.evidence_id: item.model_dump(mode="json") for item in request.evidence})
        if len(current) > 40:
            raise HTTPException(422, "An objective can retain at most 40 current evidence records.")
        objective.current_evidence = list(current.values())
    await session.flush()
    snapshot = await review_snapshot(session, objective)
    if request.workflow == "strategy_search_v1":
        versions = list((await session.scalars(select(SwarmStrategyVersion).join(SwarmReview).where(
            SwarmReview.objective_id == objective.objective_id,
        ).order_by(SwarmReview.revision.desc(), SwarmStrategyVersion.version.desc()).limit(4))).all())
        snapshot["strategy_history"] = [{"review_id": version.review_id, "branch_id": version.branch_id,
                                         "version": version.version, "candidate": version.content["candidate"],
                                         "summary": (version.content.get("attempt") or {}).get("summary", "")[:2000],
                                         "action": version.content["action"]} for version in versions]
        if len(json.dumps(snapshot).encode()) > 128_000:
            raise HTTPException(422, "Strategic context exceeds 128 KB; supply narrower evidence.")
    if request.allow_market_acquisition:
        try:
            snapshot["acquisition_scopes"] = approved_markets()
        except ValueError:
            raise HTTPException(503, "Invalid server EVE market configuration.") from None
        if not snapshot["acquisition_scopes"]:
            raise HTTPException(422, "No server-approved EVE markets are configured.")
    objective.revision += 1
    review = SwarmReview(
        review_id=str(uuid4()), objective_id=objective.objective_id, request_key=request.request_key,
        request=payload, revision=objective.revision, status="running", snapshot=snapshot,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(review)
    await session.flush()
    if request.workflow == "strategy_search_v1":
        await persist_task(session, objective, review, role="strategy_seed", graph_id="swarm_strategy",
                           inputs={"role": "seed", "snapshot": snapshot})
    else:
        for role in (("planner",) if request.workflow == "planned_review_v1" else SPECIALISTS):
            await create_task(session, objective, review, role=role, reports=[])
    append_entry(session, objective, kind="review_started", author="onyx:coordinator",
                  review_id=review.review_id, content={"revision": review.revision, "workflow": request.workflow})
    return review


async def create_task(
    session: AsyncSession, objective: SwarmObjective, review: SwarmReview, *, role: str, reports: list[dict[str, Any]],
    spec: dict[str, Any] | None = None,
    market_scope: str | None = None,
) -> SwarmTask:
    snapshot = effective_snapshot(review)
    if spec is not None:
        scope = PlannedTask.model_validate(spec)
        snapshot = {**snapshot, "evidence": [item for item in snapshot["evidence"] if item["evidence_id"] in scope.evidence_ids]}
    inputs = {"role": role, "snapshot": snapshot, "reports": reports,
              **({"spec": spec or {}, "market_scope": market_scope or ""}
                 if review.request.get("workflow") == "planned_review_v1" else {})}
    return await persist_task(session, objective, review, role=role,
                              graph_id="swarm_planner" if review.request.get("workflow") == "planned_review_v1" else GRAPH_ID,
                              inputs=inputs)


def effective_snapshot(review: SwarmReview) -> dict[str, Any]:
    if not review.acquisition:
        return review.snapshot
    return {**review.snapshot, "evidence": review.snapshot["evidence"] + review.acquisition["evidence"]}


def validate_assessment(value: Any, snapshot: dict[str, Any]) -> Assessment:
    assessment = Assessment.model_validate(value)
    allowed = {item["evidence_id"] for item in snapshot["evidence"]}
    cited = {ref for finding in assessment.findings for ref in finding.evidence_ids}
    if not cited <= allowed:
        raise ValueError("Assessment cites evidence outside the review snapshot.")
    if cited & expired_evidence(snapshot) and assessment.disposition != "needs_information":
        raise ValueError("Expired evidence requires fresh information.")
    return assessment


async def advance_review(session: AsyncSession, objective: SwarmObjective, review: SwarmReview) -> None:
    """Reconcile persisted worker outcomes once, while holding the objective lock."""
    if review.status != "running":
        return
    if objective.status != "active" or review.deadline <= datetime.now(UTC):
        review.status = "cancelled" if objective.status != "active" else "expired"
        append_entry(session, objective, kind="review_finished", author="onyx:coordinator", review_id=review.review_id,
                     content={"status": review.status})
        return
    if review.request.get("workflow") == "planned_review_v1":
        await advance_planned_review(session, objective, review)
        return
    if review.request.get("workflow") == "strategy_search_v1":
        outcome = await advance_strategy_review(session, objective, review)
        if outcome is not None:
            await finish_planned_review(session, objective, review, outcome.assessment,
                                        partial=outcome.partial, model=outcome.model)
        return
    tasks = list((await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review.review_id))).all())
    for task in tasks:
        if task.status in {"success", "error"}:
            continue
        run = await session.get(Run, task.run_id)
        if run is None or run.status not in TERMINAL_RUNS:
            continue
        task.status = "error"
        if run.status == "success":
            try:
                assessment = validate_assessment((run.output or {}).get("assessment"), review.snapshot)
            except ValueError:
                task.result = {"error": "Worker returned an invalid or out-of-scope assessment."}
            else:
                task.status, task.result = "success", assessment.model_dump(mode="json")
        else:
            task.result = {"error": "Worker execution did not succeed.", "execution_status": run.status}
        append_entry(session, objective, kind="finding" if task.status == "success" else "blocker",
                     author=f"onyx:{task.role}", review_id=review.review_id,
                     content={"task_id": task.task_id, "status": task.status, "result": task.result,
                              "producer_model": (run.output or {}).get("model")})
    if any(task.status not in {"success", "error"} for task in tasks):
        return
    synthesis = next((task for task in tasks if task.role == "coordinator"), None)
    if synthesis is None:
        reports = [{"role": task.role, "status": task.status, "result": task.result} for task in tasks]
        if all(task.status == "error" for task in tasks):
            review.status = "failed"
        else:
            await create_task(session, objective, review, role="coordinator", reports=reports)
            return
    else:
        review.status = "completed" if synthesis.status == "success" else "failed"
        if synthesis.status == "success":
            synthesis_run = await session.get(Run, synthesis.run_id)
            append_entry(session, objective, kind="assessment", author="onyx:coordinator", review_id=review.review_id,
                         content={"revision": review.revision, "assessment": synthesis.result,
                                  "partial": any(task.status == "error" for task in tasks),
                                  "simulation": any(item.get("simulation", False) for item in review.snapshot["evidence"]),
                                  "evidence_snapshot_review_id": review.review_id,
                                  "producer_model": (synthesis_run.output or {}).get("model") if synthesis_run else None})
    append_entry(session, objective, kind="review_finished", author="onyx:coordinator", review_id=review.review_id,
                 content={"status": review.status})


async def finish_planned_review(
    session: AsyncSession, objective: SwarmObjective, review: SwarmReview, assessment: dict[str, Any] | None,
    *, partial: bool = False, model: str | None = None,
) -> None:
    if assessment is not None:
        try:
            validate_assessment(assessment, {**effective_snapshot(review), "snapshot_at": datetime.now(UTC).isoformat()})
        except ValueError:
            append_entry(session, objective, kind="blocker", author="onyx:coordinator", review_id=review.review_id,
                         content={"error": "Assessment contains out-of-scope or expired evidence."})
            assessment = None
    review.status = "completed" if assessment is not None else "failed"
    if assessment is not None:
        append_entry(session, objective, kind="assessment", author="onyx:coordinator", review_id=review.review_id,
                     content={"revision": review.revision, "assessment": assessment, "partial": partial,
                              "simulation": any(e.get("simulation", False) for e in effective_snapshot(review)["evidence"]),
                              "evidence_snapshot_review_id": review.review_id, "producer_model": model,
                               "workflow": review.request.get("workflow", "planned_review_v1"),
                               "plan_decision": (review.plan or {}).get("decision")})
    append_entry(session, objective, kind="review_finished", author="onyx:coordinator",
                 review_id=review.review_id, content={"status": review.status})


async def advance_planned_review(session: AsyncSession, objective: SwarmObjective, review: SwarmReview) -> None:
    if review.acquisition:
        try:
            validate_receipt(review.acquisition, (review.plan or {})["acquire_market"], review.snapshot)
        except (ValueError, KeyError):
            append_entry(session, objective, kind="blocker", author="onyx:market_acquisition", review_id=review.review_id,
                         content={"error": "Acquired evidence expired before review publication."})
            await finish_planned_review(session, objective, review, None)
            return
    tasks = list((await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review.review_id))).all())
    for task in tasks:
        if task.status in {"success", "error"}:
            continue
        run = await session.get(Run, task.run_id)
        if run is None or run.status not in TERMINAL_RUNS:
            continue
        task.status, task.result = "error", {"error": "Worker failed or returned invalid scoped output."}
        if run.status == "success":
            try:
                output = run.output or {}
                if task.role == "planner":
                    result = validate_plan(output.get("plan"), review.snapshot).model_dump(mode="json")
                    review.plan = result
                elif task.role == "market_acquisition":
                    receipt = validate_receipt(output.get("acquisition"), (review.plan or {})["acquire_market"], review.snapshot)
                    prior = await session.scalar(select(SwarmEntry).where(
                        SwarmEntry.objective_id == objective.objective_id, SwarmEntry.kind == "evidence_acquired",
                        SwarmEntry.content["receipt_id"].astext == receipt.receipt_id,
                    ).order_by(SwarmEntry.sequence.desc()).limit(1))
                    # Keep the first retrieval timestamps on a cache hit; do not rejuvenate old evidence.
                    result = validate_receipt(prior.content if prior else receipt, receipt.scope_id, review.snapshot).model_dump(mode="json")
                    review.acquisition = result
                    if prior is None:
                        append_entry(session, objective, kind="evidence_acquired", author="onyx:market_acquisition",
                                     review_id=review.review_id, content=result)
                elif task.role == "coordinator":
                    result = validate_assessment(output.get("assessment"), effective_snapshot(review)).model_dump(mode="json")
                else:
                    spec = PlannedTask.model_validate((run.input or {}).get("spec"))
                    if spec.task_id != task.role:
                        raise ValueError("Worker task ID mismatch.")
                    if spec.capability == "stock_calculation":
                        record = next(e for e in effective_snapshot(review)["evidence"] if e["evidence_id"] == spec.evidence_ids[0])
                        result = stock_calculation(record)
                        if output.get("calculation") != result:
                            raise ValueError("Calculation receipt mismatch.")
                    else:
                        result = validate_assessment(output.get("assessment"), (run.input or {})["snapshot"]).model_dump(mode="json")
            except (ValueError, KeyError, StopIteration):
                task.result = {"error": "Worker output failed plan, evidence-scope, or calculation validation."}
            else:
                task.status, task.result = "success", result
        append_entry(session, objective, kind="plan" if task.role == "planner" and task.status == "success" else
                     "finding" if task.status == "success" else "blocker", author=f"onyx:{task.role}",
                     review_id=review.review_id, content={"task_id": task.task_id, "status": task.status,
                                                         "result": task.result, "producer_model": (run.output or {}).get("model")})
    planner = next(t for t in tasks if t.role == "planner")
    if planner.status not in {"success", "error"}:
        return
    if planner.status == "error":
        await finish_planned_review(session, objective, review, None)
        return
    plan = validate_plan(review.plan, review.snapshot)
    if plan.decision != "tasks":
        planner_run = await session.get(Run, planner.run_id)
        await finish_planned_review(session, objective, review, plan.assessment.model_dump(mode="json") if plan.assessment else None,
                                    model=(planner_run.output or {}).get("model") if planner_run else None)
        return
    by_role = {t.role: t for t in tasks}
    if plan.acquire_market is not None:
        acquisition = by_role.get("market_acquisition")
        if acquisition is None:
            await create_task(session, objective, review, role="market_acquisition", reports=[], market_scope=plan.acquire_market)
            return
        if acquisition.status not in {"success", "error"}:
            return
        if acquisition.status == "error":
            await finish_planned_review(session, objective, review, None)
            return
    created = False
    for spec in plan.tasks:
        if spec.task_id not in by_role and all(dep in by_role and by_role[dep].status in {"success", "error"} for dep in spec.depends_on):
            reports = [{"role": dep, "status": by_role[dep].status, "result": by_role[dep].result} for dep in spec.depends_on]
            await create_task(session, objective, review, role=spec.task_id, reports=reports, spec=spec.model_dump(mode="json"))
            created = True
    if created or any(spec.task_id not in by_role or by_role[spec.task_id].status not in {"success", "error"} for spec in plan.tasks):
        return
    specialists = [by_role[spec.task_id] for spec in plan.tasks]
    synthesis = by_role.get("coordinator")
    if synthesis is None:
        if all(t.status == "error" for t in specialists):
            await finish_planned_review(session, objective, review, None)
        else:
            await create_task(session, objective, review, role="coordinator",
                              reports=[{"role": t.role, "status": t.status, "result": t.result} for t in specialists])
        return
    if synthesis.status in {"success", "error"}:
        synthesis_run = await session.get(Run, synthesis.run_id)
        await finish_planned_review(session, objective, review, synthesis.result if synthesis.status == "success" else None,
                                    partial=any(t.status == "error" for t in specialists),
                                    model=(synthesis_run.output or {}).get("model") if synthesis_run else None)


async def set_objective_status(session: AsyncSession, objective: SwarmObjective, status: str, user: User) -> None:
    if objective.status == status:
        return
    objective.status = status
    append_entry(session, objective, kind="objective_status", author=user.identity, content={"status": status})
    if status != "active":
        supervisor = await session.get(SwarmSupervisor, objective.objective_id)
        if supervisor is not None and supervisor.enabled:
            supervisor.enabled, supervisor.next_wakeup, supervisor.wake_pending = False, None, False
            supervisor.state, supervisor.reason = "stopped", "Objective paused. Explicit start required after resume."
            append_entry(session, objective, kind="supervisor_state", author=user.identity,
                         content={"generation": supervisor.generation, "state": "stopped", "reason": supervisor.reason})
        reviews = (await session.scalars(select(SwarmReview).where(
            SwarmReview.objective_id == objective.objective_id, SwarmReview.status == "running",
        ))).all()
        for review in reviews:
            review.status = "cancelled"
            append_entry(session, objective, kind="review_finished", author=user.identity, review_id=review.review_id,
                         content={"status": "cancelled"})
