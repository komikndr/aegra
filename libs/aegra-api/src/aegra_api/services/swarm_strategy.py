"""Recoverable two-branch, two-pass strategic search on existing RunJobs."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.core.orm import Run
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmStrategyVersion, SwarmTask
from aegra_api.models.swarm_coordination import Assessment
from aegra_api.models.swarm_strategy import StrategyCritique, StrategyDecision, StrategySeed
from aegra_api.services.swarm_acquisition import validate_receipt
from aegra_api.services.swarm_capabilities import expired_evidence, stock_calculation
from aegra_api.services.swarm_task_io import append_entry, persist_task

TERMINAL = frozenset({"success", "error", "timeout", "interrupted", "cancelled"})
MAX_PASSES = 2
MODEL_RESERVATION = 11
SPECIALIST_RESERVATION = 8


@dataclass(frozen=True)
class SearchOutcome:
    assessment: dict[str, Any] | None
    partial: bool = False
    model: str | None = None


def scoped_assessment(value: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    assessment = Assessment.model_validate(value)
    cited = {ref for finding in assessment.findings for ref in finding.evidence_ids}
    if not cited <= {item["evidence_id"] for item in snapshot["evidence"]}:
        raise ValueError("Strategy cites unavailable evidence.")
    if cited & expired_evidence({**snapshot, "snapshot_at": datetime.now(UTC).isoformat()}) and assessment.disposition != "needs_information":
        raise ValueError("Strategy uses expired evidence.")
    return assessment.model_dump(mode="json")


def verify_investigations(critique: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Actual deterministic results, including explicit unsupported/missing-input errors."""
    parsed = StrategyCritique.model_validate(critique)
    evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
    receipts: list[dict[str, Any]] = []
    for request in parsed.investigations:
        if request.evidence_id not in evidence:
            raise ValueError("Investigation refers to unavailable evidence.")
        receipt: dict[str, Any] = {"request": request.model_dump(mode="json"), "producer": "onyx:deterministic-verifier"}
        if request.kind == "evidence_freshness":
            receipt.update(status="checked", calculation_version="explicit_expiry_v1",
                           explicitly_expired=request.evidence_id in expired_evidence(snapshot),
                           limitation="Absence of explicit expiry does not establish freshness.")
        else:
            try:
                receipt.update(status="checked", result=stock_calculation(evidence[request.evidence_id]))
            except ValueError:
                receipt.update(status="unavailable", error="Evidence lacks valid structured stock inputs.")
        receipts.append(receipt)
    return receipts


async def advance_strategy_review(session: AsyncSession, objective: SwarmObjective, review: SwarmReview) -> SearchOutcome | None:
    """Caller holds the objective lock and has checked pause, supersession and deadline."""
    snapshot: dict[str, Any] = {**review.snapshot, "snapshot_at": datetime.now(UTC).isoformat()}
    if review.acquisition:
        try:
            validate_receipt(review.acquisition, (review.plan or {})["acquire_market"], review.snapshot)
        except (ValueError, KeyError):
            append_entry(session, objective, kind="blocker", author="onyx:strategy", review_id=review.review_id,
                         content={"error": "Acquired evidence expired during strategic search."})
            return SearchOutcome(None)
        snapshot["evidence"] = review.snapshot["evidence"] + review.acquisition["evidence"]
    tasks = list((await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review.review_id))).all())
    by_role = {task.role: task for task in tasks}
    for task in tasks:
        if task.status in {"success", "error"}:
            continue
        run = await session.get(Run, task.run_id)
        if run is None or run.status not in TERMINAL:
            continue
        task.status, task.result = "error", {"error": "Strategy worker failed or returned invalid output."}
        if run.status == "success":
            try:
                output = run.output or {}
                stage = (run.input or {}).get("role")
                if stage == "seed":
                    seed = StrategySeed.model_validate(output.get("seed"))
                    if seed.acquire_market and seed.acquire_market not in review.snapshot.get("acquisition_scopes", {}):
                        raise ValueError("Unapproved acquisition scope.")
                    prospective = {f"eve:{seed.acquire_market}:quote", f"eve:{seed.acquire_market}:history"}
                    if seed.acquire_market and prospective & {e["evidence_id"] for e in snapshot["evidence"]}:
                        raise ValueError("Acquisition would shadow supplied evidence.")
                    result = seed.model_dump(mode="json")
                    review.plan = result
                elif stage == "acquire":
                    receipt = validate_receipt(output.get("acquisition"), (review.plan or {})["acquire_market"], review.snapshot)
                    prior = await session.scalar(select(SwarmEntry).where(
                        SwarmEntry.objective_id == objective.objective_id, SwarmEntry.kind == "evidence_acquired",
                        SwarmEntry.content["receipt_id"].astext == receipt.receipt_id,
                    ).order_by(SwarmEntry.sequence.desc()).limit(1))
                    result = validate_receipt(prior.content if prior else receipt, receipt.scope_id, review.snapshot).model_dump(mode="json")
                    review.acquisition = result
                    if prior is None:
                        append_entry(session, objective, kind="evidence_acquired", author="onyx:strategy", review_id=review.review_id, content=result)
                elif stage == "develop":
                    result = scoped_assessment(output.get("assessment"), snapshot)
                elif stage == "critique":
                    critique = StrategyCritique.model_validate(output.get("critique"))
                    verify_investigations(critique.model_dump(mode="json"), snapshot)
                    result = critique.model_dump(mode="json")
                elif stage == "verify":
                    # Recompute against the exact dispatched snapshot; do not trust model or worker labels.
                    result = {"receipts": verify_investigations((run.input or {})["critique"], (run.input or {})["snapshot"])}
                    if result != output.get("verification"):
                        raise ValueError("Verification receipt mismatch.")
                elif stage == "decide":
                    decision = StrategyDecision.model_validate(output.get("decision"))
                    scoped_assessment(decision.assessment, snapshot)
                    expected = {item["candidate"]["branch_id"] for item in (run.input or {})["reports"]}
                    if {action.branch_id for action in decision.actions} != expected:
                        raise ValueError("Controller must decide each active branch exactly once.")
                    result = decision.model_dump(mode="json")
                else:
                    raise ValueError("Unknown strategy stage.")
            except (ValueError, KeyError):
                pass  # Retain the explicit error outcome initialized above; no fallback result.
            else:
                task.status, task.result = "success", result
        append_entry(session, objective, kind="strategy_result" if task.status == "success" else "blocker",
                     author=f"onyx:{task.role}", review_id=review.review_id,
                     content={"task_id": task.task_id, "run_id": task.run_id, "status": task.status,
                              "result": task.result, "producer_model": (run.output or {}).get("model")})
    if any(task.status not in {"success", "error"} for task in tasks):
        return None
    seed_task = by_role["strategy_seed"]
    if seed_task.status == "error":
        return SearchOutcome(None)
    seed = StrategySeed.model_validate(review.plan)
    if seed.acquire_market and "strategy_acquire" not in by_role:
        await persist_task(session, objective, review, role="strategy_acquire", graph_id="swarm_strategy",
                           inputs={"role": "acquire", "snapshot": review.snapshot, "market_scope": seed.acquire_market})
        return None
    if seed.acquire_market and by_role["strategy_acquire"].status == "error":
        return SearchOutcome(None)
    if seed.acquire_market and review.acquisition:
        snapshot["evidence"] = review.snapshot["evidence"] + review.acquisition["evidence"]
    candidates = list(seed.candidates)
    previous: dict[str, dict[str, Any]] = {}
    for pass_number in range(1, MAX_PASSES + 1):
        reports: list[dict[str, Any]] = []
        created = False
        for candidate in candidates:
            prefix = f"p{pass_number}_{candidate.branch_id}"
            develop, critique, verify = (by_role.get(f"{prefix}_{stage}") for stage in ("develop", "critique", "verify"))
            inputs = {"snapshot": snapshot, "candidate": candidate.model_dump(mode="json"),
                      "previous": previous.get(candidate.branch_id, {})}
            if develop is None:
                await persist_task(session, objective, review, role=f"{prefix}_develop", graph_id="swarm_strategy", inputs={**inputs, "role": "develop"})
                created = True
                continue
            if develop.status == "success" and critique is None:
                await persist_task(session, objective, review, role=f"{prefix}_critique", graph_id="swarm_strategy",
                                   inputs={**inputs, "role": "critique", "attempt": develop.result})
                created = True
                continue
            if critique is not None and critique.status == "success" and verify is None:
                await persist_task(session, objective, review, role=f"{prefix}_verify", graph_id="swarm_strategy",
                                   inputs={**inputs, "role": "verify", "critique": critique.result})
                created = True
                continue
            reports.append({"candidate": candidate.model_dump(mode="json"),
                            "execution_refs": {"develop": develop.run_id, "critique": critique.run_id if critique else None,
                                               "verify": verify.run_id if verify else None},
                            "attempt": develop.result, "attempt_status": develop.status,
                            "critique": critique.result if critique else None,
                            "verification": verify.result if verify else None,
                            "complete": develop.status == "success" and critique is not None and critique.status == "success"
                            and verify is not None and verify.status == "success"})
        if created:
            return None
        if not any(report["complete"] for report in reports):
            return SearchOutcome(None)
        controller = by_role.get(f"p{pass_number}_decide")
        if controller is None:
            await persist_task(session, objective, review, role=f"p{pass_number}_decide", graph_id="swarm_strategy",
                               inputs={"role": "decide", "snapshot": snapshot, "reports": reports,
                                       "pass_number": pass_number, "remaining_passes": MAX_PASSES - pass_number})
            return None
        if controller.status == "error":
            return SearchOutcome(None)
        decision = StrategyDecision.model_validate(controller.result)
        for report in reports:
            branch = report["candidate"]["branch_id"]
            action = next(action for action in decision.actions if action.branch_id == branch)
            content = {**report, "action": action.model_dump(mode="json"),
                       "parent_version": {"review_id": review.review_id, "branch_id": branch, "version": pass_number - 1}
                       if pass_number > 1 else None}
            key = (review.review_id, branch, pass_number)
            if await session.get(SwarmStrategyVersion, key) is None:
                session.add(SwarmStrategyVersion(review_id=review.review_id, branch_id=branch, version=pass_number, content=content))
                append_entry(session, objective, kind="strategy_version", author="onyx:strategy", review_id=review.review_id,
                             content={"branch_id": branch, "version": pass_number, "action": action.model_dump(mode="json")})
            previous[branch] = content
        continuing = {action.branch_id for action in decision.actions if action.action in {"continue", "revise"}}
        if not continuing or pass_number == MAX_PASSES:
            assessment = decision.assessment.model_dump(mode="json")
            exhausted = bool(continuing)
            if exhausted:
                assessment["uncertainties"].append("Strategic search reached its two-pass budget with unresolved branches.")
            run = await session.get(Run, controller.run_id)
            return SearchOutcome(assessment, partial=exhausted or any(not report["complete"] for report in reports),
                                 model=(run.output or {}).get("model") if run else None)
        candidates = [candidate for candidate in candidates if candidate.branch_id in continuing]
    return SearchOutcome(None)
